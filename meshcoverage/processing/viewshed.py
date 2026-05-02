"""
Calcolo viewshed (analisi di visibilità) parallelizzato.

  DEM in SharedMemory — l'intera area di analisi viene caricata in un
      array numpy nel processo padre e condivisa con i worker via
      multiprocessing.shared_memory.  I worker non aprono più rasterio né
      reinizializzano DEMHandler: accedono direttamente alla RAM condivisa.
        Algoritmo:
        1. Genera una griglia di punti target nell'area di interesse
        2. Per ogni punto target, estrae il profilo di elevazione DEM
        3. Verifica LOS e clearance zona di Fresnel
        4. Calcola link budget per i punti raggiungibili
        5. Salva risultati in formato compresso (.npz)
            — include anche le shadow zone (punti senza LOS)

  Lettura windowed — load_area_array() esegue un singolo reproject/read
      per dataset invece di N letture puntuali (Window(col,row,1,1)).
      Il profilo di elevazione è estratto con interpolazione bilineare
      vettorizzata sull'array numpy in memoria.

  Pre-filtro free-space — prima di qualsiasi accesso DEM, ogni worker
      calcola il margine di link budget in condizioni di spazio libero ideale.
      Se il margine è già inferiore a -20 dB rispetto alla sensibilità, il
      punto viene scartato immediatamente (nessuna propagazione RF possibile
      nemmeno con LOS perfetto).

  NOTE ( TODO — non implementato):
      Per ambienti urbani o forestati, l'accuratezza potrebbe essere migliorata
      sostituendo il DTM bare-earth con un DSM (Digital Surface Model) che
      include l'altezza di edifici e alberi sopra il terreno.
"""
from __future__ import annotations
import logging
import math
import multiprocessing as mp
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from meshcoverage.processing.dem_handler import (
    haversine_m, bearing_deg, earth_bulge_m, destination_point,
    EARTH_RADIUS_M,
)
from meshcoverage.processing.fresnel import check_los, check_fresnel_clearance
from meshcoverage.processing.link_budget import (
    calculate_link_budget, fspl_db, atmospheric_loss_db,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-worker shared state (populated once by _worker_init)
# ---------------------------------------------------------------------------

_worker_state: dict = {}


def _worker_init(
    shm_name: str,
    shape: tuple,
    dtype_str: str,
    dem_meta: dict,
    sensitivity_dbm: float,
) -> None:
    """
    Called once per worker process by multiprocessing.Pool.
    Attaches to the SharedMemory block created in the parent process and
    stores a numpy view + metadata in the module-level _worker_state dict.
    Workers never open rasterio datasets — they read from shared RAM only.
    """
    from multiprocessing.shared_memory import SharedMemory

    global _worker_state
    shm = SharedMemory(name=shm_name, create=False)
    arr = np.ndarray(shape, dtype=np.dtype(dtype_str), buffer=shm.buf)
    _worker_state = {
        'shm': shm,           # keep reference alive for the lifetime of the worker
        'arr': arr,
        'meta': dem_meta,
        'sensitivity_dbm': sensitivity_dbm,
    }


# ---------------------------------------------------------------------------
# Array-based elevation helpers (run inside worker processes)
# ---------------------------------------------------------------------------

def _arr_elevation(lat: float, lon: float) -> Optional[float]:
    """
    Bilinear interpolation of elevation at (lat, lon) from the shared array.
    Returns None when the coordinate is outside the array or the value is NaN.
    """
    meta = _worker_state['meta']
    arr  = _worker_state['arr']

    if (lat < meta['lat_min'] or lat > meta['lat_max'] or
            lon < meta['lon_min'] or lon > meta['lon_max']):
        return None

    row = (meta['lat_max'] - lat) / (meta['lat_max'] - meta['lat_min']) * (meta['n_rows'] - 1)
    col = (lon - meta['lon_min']) / (meta['lon_max'] - meta['lon_min']) * (meta['n_cols'] - 1)

    r0 = max(0, min(int(row), meta['n_rows'] - 2))
    c0 = max(0, min(int(col), meta['n_cols'] - 2))
    fr, fc = row - r0, col - c0

    v = (arr[r0,     c0    ] * (1 - fr) * (1 - fc) +
         arr[r0 + 1, c0    ] * fr       * (1 - fc) +
         arr[r0,     c0 + 1] * (1 - fr) * fc +
         arr[r0 + 1, c0 + 1] * fr       * fc)

    v = float(v)
    return None if (math.isnan(v) or v < -1000.0) else v


def _arr_profile(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    num_points: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized elevation profile from the shared array using bilinear
    interpolation.  No Python loops, no rasterio calls.

    Returns (distances_m, lats, elevations) — same signature as
    DEMHandler.get_profile() so the rest of the algorithm is unchanged.
    """
    meta = _worker_state['meta']
    arr  = _worker_state['arr']

    lats = np.linspace(lat1, lat2, num_points)
    lons = np.linspace(lon1, lon2, num_points)

    # Vectorised haversine distances from TX (avoids a Python loop)
    dlat = np.radians(lats - lat1)
    dlon = np.radians(lons - lon1)
    a = (np.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * np.cos(np.radians(lats)) *
         np.sin(dlon / 2) ** 2)
    distances_m = 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))

    # Convert lat/lon → fractional array indices
    lat_range = meta['lat_max'] - meta['lat_min']
    lon_range = meta['lon_max'] - meta['lon_min']

    rows = (meta['lat_max'] - lats) / lat_range * (meta['n_rows'] - 1)
    cols = (lons  - meta['lon_min']) / lon_range * (meta['n_cols'] - 1)

    # Mark out-of-bounds before clamping so we can NaN them later
    oob = (
        (lats < meta['lat_min']) | (lats > meta['lat_max']) |
        (lons < meta['lon_min']) | (lons > meta['lon_max'])
    )

    rows = np.clip(rows, 0, meta['n_rows'] - 2)
    cols = np.clip(cols, 0, meta['n_cols'] - 2)

    r0 = rows.astype(np.int32)
    c0 = cols.astype(np.int32)
    fr = rows - r0
    fc = cols - c0

    # Bilinear interpolation (fully vectorised)
    elevations = (
        arr[r0,     c0    ] * (1.0 - fr) * (1.0 - fc) +
        arr[r0 + 1, c0    ] * fr         * (1.0 - fc) +
        arr[r0,     c0 + 1] * (1.0 - fr) * fc +
        arr[r0 + 1, c0 + 1] * fr         * fc
    ).astype(np.float64)

    # Mask invalid cells
    elevations[oob | (elevations < -1000.0)] = np.nan

    return distances_m, lats, elevations


# ---------------------------------------------------------------------------
# Worker function
# ---------------------------------------------------------------------------

def _compute_point(args: tuple) -> Optional[dict]:
    """
    Returns a dict with los=True (covered) or los=False (shadow zone).
    Always returns a result for valid terrain points so shadow zones
    can be recorded.
    """
    (
        target_lat, target_lon,
        ant_lat, ant_lon, ant_alt_m,
        rx_height_m, rx_gain_dbi,
        freq_mhz, modem_preset,
        tx_power_dbm, ant_gain_dbi,
        ant_azimuth, ant_beamwidth,
        ant_gain_min, ant_gain_max,
    ) = args

    try:
        dist_m = haversine_m(ant_lat, ant_lon, target_lat, target_lon)
        if dist_m < 10.0:
            return None

        # ── free-space pre-filter ───────────────────────────────
        # Compute the best-case link margin (free space, no obstacles,
        # full antenna gain).  If it is already < -20 dB below sensitivity,
        # no terrain model will save this point — skip it entirely.
        _fspl = fspl_db(dist_m, freq_mhz)
        _atm  = atmospheric_loss_db(dist_m, freq_mhz)
        sensitivity_dbm = _worker_state.get('sensitivity_dbm', -140.0)
        best_case_margin = (
            tx_power_dbm + ant_gain_dbi + rx_gain_dbi
            - _fspl - _atm - sensitivity_dbm
        )
        if best_case_margin < -20.0:
            return None
        # ──────────────────────────────────────────────────────────────────

        brng = bearing_deg(ant_lat, ant_lon, target_lat, target_lon)

        # Beamwidth sector check
        if ant_beamwidth < 360.0:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            if diff > ant_beamwidth / 2.0:
                return None  # Fuori dal fascio — non è shadow zone, è semplicemente fuori settore

        # Directional antenna gain at this bearing
        if ant_beamwidth >= 360.0:
            gain_used = ant_gain_dbi
        else:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            half_bw = ant_beamwidth / 2.0
            factor = max(0.0, 1.0 - diff / half_bw)
            gain_used = ant_gain_min + factor * (ant_gain_max - ant_gain_min)

        # ── array-based DEM lookups ───────────────────────────
        target_elev = _arr_elevation(target_lat, target_lon)
        if target_elev is None:
            return None # Nessun dato DEM — non possiamo determinare se shadow
        rx_alt_m = target_elev + rx_height_m 

        n_samples = max(50, int(dist_m / 30))
        distances_m, lats, elevations = _arr_profile(
            ant_lat, ant_lon, target_lat, target_lon, n_samples
        )
        # ──────────────────────────────────────────────────────────────────

        # Earth bulge correction (vectorised)
        bulge = np.array([earth_bulge_m(d) for d in distances_m])
        elevations_corr = np.where(np.isnan(elevations), np.nan, elevations + bulge)

        # LOS check
        los_ok, _ = check_los(
            distances_m, elevations_corr,
            ant_alt_m, rx_alt_m, dist_m,
            apply_earth_bulge=False,
        )
        if not los_ok:
            # Shadow zone: terreno blocca la visibilità
            return {
                "lat": target_lat, "lon": target_lon,
                "distance_m": dist_m, "los": False,
                "fresnel_ok": False, "link_budget_db": float("-inf"),
                "bearing": brng,
                "shadow": True,  # <-- marcatore shadow zone
            }

        # Fresnel clearance check
        fresnel_ok, _ = check_fresnel_clearance(
            distances_m, elevations_corr,
            ant_alt_m, rx_alt_m, dist_m, freq_mhz,
        )

        diffraction_loss = 0.0 if fresnel_ok else 6.0
        lb = calculate_link_budget(
            dist_m, freq_mhz, modem_preset,
            tx_power_dbm, gain_used, rx_gain_dbi,
            additional_loss_db=diffraction_loss,
        )

        return {
            "lat": target_lat, "lon": target_lon,
            "distance_m": dist_m, "los": True,
            "fresnel_ok": fresnel_ok,
            "link_budget_db": lb["link_margin_db"],
            "bearing": brng,
            "shadow": False,
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ViewshedParams:
    """Parametri per il calcolo viewshed di un singolo nodo."""
    node_id: str
    ant_lat: float
    ant_lon: float
    ant_alt_m: float
    ground_height_m: float
    freq_mhz: int
    modem_preset: str
    tx_power_dbm: float
    ant_gain_dbi: float
    ant_azimuth_deg: float
    ant_beamwidth_deg: float
    ant_gain_min_dbi: float
    ant_gain_max_dbi: float
    rx_gain_dbi: float
    max_range_m: float
    dem_dir: Path
    resolution_m: float
    rx_height_m: float


@dataclass
class ViewshedResult:
    """Risultato del calcolo viewshed per un punto target."""
    lat: float
    lon: float
    distance_m: float
    los: bool
    fresnel_ok: bool
    link_budget_db: float
    bearing_deg: float


# ---------------------------------------------------------------------------
# Grid generation
# ---------------------------------------------------------------------------

def generate_target_grid(
    center_lat: float, center_lon: float,
    max_range_m: float, resolution_m: float,
    ant_azimuth: float = 0.0, ant_beamwidth: float = 360.0,
) -> list[tuple[float, float]]:
    """
    Genera la griglia di punti target nell'area di interesse.
    Itera su tutti i punti della griglia senza settori angolari fissi.
    """
    # Dimensioni griglia in gradi
    lat_step = resolution_m / 111_000.0
    lon_step = resolution_m / (111_000.0 * math.cos(math.radians(center_lat)))
    n_steps  = int(max_range_m / resolution_m) + 1

    points = []
    for i in range(-n_steps, n_steps + 1):
        for j in range(-n_steps, n_steps + 1):
            lat = center_lat + i * lat_step
            lon = center_lon + j * lon_step

            dist = haversine_m(center_lat, center_lon, lat, lon)
            if dist > max_range_m or dist < resolution_m / 2:
                continue

            if ant_beamwidth < 360.0:
                brng = bearing_deg(center_lat, center_lon, lat, lon)
                diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
                if diff > ant_beamwidth / 2.0 + 10:
                    continue

            points.append((lat, lon))

    return points


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_viewshed(
    params: ViewshedParams,
    n_workers: int = 0,
    progress_callback=None,
) -> list[dict]:
    """
    Calcola il viewshed completo per un nodo.
    Restituisce la lista dei punti con i risultati (sia coperti che shadow zone).
    
    Args:
        params: parametri del calcolo
        n_workers: numero di processi paralleli (0=auto)
        progress_callback: callable(done, total) per aggiornamento progresso
    """
    from multiprocessing.shared_memory import SharedMemory
    from meshcoverage.processing.dem_handler import get_dem_handler
    from meshcoverage.models.node import MODEM_PRESETS

    if n_workers <= 0:
        n_workers = max(1, os.cpu_count() or 1)

    log.info(
        f"Viewshed {params.node_id}: "
        f"range={params.max_range_m / 1000:.1f}km "
        f"res={params.resolution_m}m "
        f"workers={n_workers}"
    )

    # ── Step 1: load DEM area into numpy array ──────────────────────
    dem = get_dem_handler()

    # Bounding box with a small margin so edge points have a full profile
    margin_deg = (params.max_range_m / 111_000.0) * 1.05
    margin_lon = margin_deg / max(0.01, math.cos(math.radians(params.ant_lat)))
    lat_min = params.ant_lat - margin_deg
    lat_max = params.ant_lat + margin_deg
    lon_min = params.ant_lon - margin_lon
    lon_max = params.ant_lon + margin_lon

    log.info(
        f"Viewshed {params.node_id}: pre-caricamento DEM "
        f"[{lat_min:.3f},{lon_min:.3f}]→[{lat_max:.3f},{lon_max:.3f}]"
    )

    dem_arr, dem_meta = dem.load_area_array(
        lat_min, lon_min, lat_max, lon_max,
        resolution_m=params.resolution_m,
    )

    if dem_arr is None:
        log.error(
            f"Viewshed {params.node_id}: nessun dato DEM nell'area, "
            f"impossibile calcolare"
        )
        return []

    # ── Step 2: copy array into SharedMemory ──────────────────────────
    shm = SharedMemory(create=True, size=int(dem_arr.nbytes))
    shm_view = np.ndarray(dem_arr.shape, dtype=dem_arr.dtype, buffer=shm.buf)
    shm_view[:] = dem_arr[:]
    del dem_arr          # release local copy; shared copy is in shm

    # Receiver sensitivity for the pre-filter
    preset_data     = MODEM_PRESETS.get(params.modem_preset, {})
    sensitivity_dbm = preset_data.get("receiver_sensitivity_dbm", -140.0)

    # ── Generate target grid ───────────────────────────────────────────────
    targets = generate_target_grid(
        params.ant_lat, params.ant_lon,
        params.max_range_m, params.resolution_m,
        params.ant_azimuth_deg, params.ant_beamwidth_deg,
    )
    log.info(f"Viewshed {params.node_id}: {len(targets)} punti target da analizzare")

    if not targets:
        shm.close()
        shm.unlink()
        return []

    ant_gain_min = (
        params.ant_gain_min_dbi
        if params.ant_gain_min_dbi is not None
        else params.ant_gain_dbi - 3
    )
    ant_gain_max = (
        params.ant_gain_max_dbi
        if params.ant_gain_max_dbi is not None
        else params.ant_gain_dbi
    )

    worker_args = [
        (
            lat, lon,
            params.ant_lat, params.ant_lon, params.ant_alt_m,
            params.rx_height_m, params.rx_gain_dbi,
            params.freq_mhz, params.modem_preset,
            params.tx_power_dbm, params.ant_gain_dbi,
            params.ant_azimuth_deg, params.ant_beamwidth_deg,
            ant_gain_min, ant_gain_max,
        )
        for lat, lon in targets
    ]

    results = []
    done    = 0
    total   = len(worker_args)

    # ── Step 3+4: worker pool with shared-memory initialiser ─────────
    try:
        with mp.Pool(
            processes=n_workers,
            initializer=_worker_init,
            initargs=(
                shm.name,
                shm_view.shape,
                str(shm_view.dtype),
                dem_meta,
                sensitivity_dbm,
            ),
        ) as pool:
            # chunksize=50 amortises IPC overhead; workers are fast now that
            # DEM I/O is eliminated.
            for result in pool.imap_unordered(
                _compute_point, worker_args, chunksize=50
            ):
                done += 1
                if result is not None:
                    results.append(result)
                if progress_callback and done % 100 == 0:
                    progress_callback(done, total)
    finally:
        # ── Step 5: always release SharedMemory ───────────────────────
        shm.close()
        shm.unlink()

    if progress_callback:
        progress_callback(total, total)

    covered = len([r for r in results if r.get("los")])
    shadow = len([r for r in results if r.get("shadow")])
    log.info(
        f"Viewshed {params.node_id}: {covered} punti coperti, "
        f"{shadow} shadow zone "
        f"({len([r for r in results if r.get('fresnel_ok')])} con Fresnel ok)"
    )
    return results


# ---------------------------------------------------------------------------
# Persistence helpers (unchanged)
# ---------------------------------------------------------------------------

def save_viewshed(results: list[dict], output_path: Path, metadata: dict):
    """
    Salva i risultati del viewshed in formato NPZ compresso.
    Separate arrays for covered points and shadow zones.
    """
    if not results:
        np.savez_compressed(
            output_path,
            # Covered points
            lats=np.array([]),
            lons=np.array([]),
            distances=np.array([]),
            los=np.array([], dtype=bool),
            fresnel_ok=np.array([], dtype=bool),
            link_budget=np.array([]),
            bearings=np.array([]),
            # Shadow zones
            shadow_lats=np.array([]),
            shadow_lons=np.array([]),
            shadow_distances=np.array([]),
            metadata=str(metadata),
        )
        return

    # Separate covered vs shadow
    covered = [r for r in results if r.get("los", False)]
    shadow_pts = [r for r in results if r.get("shadow", False)]

    # Covered point arrays
    lats = np.array([r["lat"] for r in covered]) if covered else np.array([])
    lons = np.array([r["lon"] for r in covered]) if covered else np.array([])
    distances = np.array([r["distance_m"] for r in covered]) if covered else np.array([])
    los = np.ones(len(covered), dtype=bool)
    fresnel = np.array([r["fresnel_ok"] for r in covered], dtype=bool) if covered else np.array([], dtype=bool)
    lb = np.array([r["link_budget_db"] for r in covered]) if covered else np.array([])
    bearings = np.array([r["bearing"] for r in covered]) if covered else np.array([])

    # Shadow zone arrays
    shadow_lats = np.array([r["lat"] for r in shadow_pts]) if shadow_pts else np.array([])
    shadow_lons = np.array([r["lon"] for r in shadow_pts]) if shadow_pts else np.array([])
    shadow_distances = np.array([r["distance_m"] for r in shadow_pts]) if shadow_pts else np.array([])

    np.savez_compressed(
        output_path,
        lats=lats, lons=lons,
        distances=distances,
        los=los, fresnel_ok=fresnel,
        link_budget=lb,
        bearings=bearings,
        shadow_lats=shadow_lats,
        shadow_lons=shadow_lons,
        shadow_distances=shadow_distances,
        metadata=str(metadata),
    )
    log.info(
        f"Viewshed salvato: {output_path} "
        f"({len(covered)} coperti, {len(shadow_pts)} shadow zone)"
    )


def load_viewshed(path: Path) -> Optional[dict]:
    """
    Carica i risultati di un viewshed salvato.
    Returns dict with both covered and shadow_zone arrays.
    Shadow arrays may be empty for files computed before this update.
    """
    if not path.exists():
        return None
    try:
        data = np.load(path, allow_pickle=True)
        return {
            # Covered / reachable points
            "lats": data["lats"],
            "lons": data["lons"],
            "distances": data["distances"],
            "los": data["los"],
            "fresnel_ok": data["fresnel_ok"],
            "link_budget": data["link_budget"],
            "bearings": data.get("bearings", np.array([])),
            # Shadow zones (may not exist in older files)
            "shadow_lats": data["shadow_lats"] if "shadow_lats" in data else np.array([]),
            "shadow_lons": data["shadow_lons"] if "shadow_lons" in data else np.array([]),
            "shadow_distances": data["shadow_distances"] if "shadow_distances" in data else np.array([]),
        }
    except Exception as e:
        log.error(f"Errore caricamento viewshed {path}: {e}")
        return None
