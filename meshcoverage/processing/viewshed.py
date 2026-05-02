"""
Calcolo viewshed (analisi di visibilità) parallelizzato.

Ottimizzazioni e funzionalità:

  DEM/DSM in SharedMemory — l'intera area di analisi viene caricata in
      array numpy nel processo padre e condivisa con i worker via
      multiprocessing.shared_memory.  I worker non aprono rasterio.

  Lettura windowed — load_area_array() esegue un singolo reproject/read
      per dataset.  Il profilo di elevazione è estratto con interpolazione
      bilineare vettorizzata sull'array numpy in memoria.

  DSM clutter layer — quando MESHCOVERAGE_DSM_DIR è configurato, le
      altezze degli ostacoli lungo il percorso (edifici, alberi) sono lette
      dal DSM (Digital Surface Model) anziché dal DTM bare-earth.
      Le elevazioni TX e RX continuano a usare il DTM in modo che l'altezza
      dell'antenna dal suolo e i 1.5 m del ricevitore siano misurati
      correttamente.
      Se il DSM non copre un punto del profilo, si cade in fallback sul DTM.

  Pre-filtro free-space — prima di qualsiasi accesso DEM, ogni worker
      calcola il margine di link budget in condizioni di spazio libero ideale.
      Se è già < -20 dB il punto viene scartato senza toccare gli array.
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
    haversine_m, bearing_deg, earth_bulge_m, EARTH_RADIUS_M,
    get_dem_handler, get_dsm_handler,
)
from meshcoverage.processing.fresnel import check_los, check_fresnel_clearance
from meshcoverage.processing.link_budget import (
    calculate_link_budget, fspl_db, atmospheric_loss_db,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-worker shared state
# ---------------------------------------------------------------------------

_worker_state: dict = {}


def _worker_init(
    # DTM shared memory (always present)
    dtm_shm_name: str,
    dtm_shape: tuple,
    dtm_dtype: str,
    dtm_meta: dict,
    # DSM shared memory (optional — empty string when not available)
    dsm_shm_name: str,
    dsm_shape: tuple,
    dsm_dtype: str,
    dsm_meta: Optional[dict],
    # Preset sensitivity for pre-filter (H)
    sensitivity_dbm: float,
) -> None:
    """
    Called once per worker process.
    Attaches to both SharedMemory blocks (DTM always, DSM when available).
    """
    from multiprocessing.shared_memory import SharedMemory

    global _worker_state

    dtm_shm = SharedMemory(name=dtm_shm_name, create=False)
    dtm_arr = np.ndarray(dtm_shape, dtype=np.dtype(dtm_dtype), buffer=dtm_shm.buf)

    dsm_shm = dsm_arr = None
    if dsm_shm_name:
        dsm_shm = SharedMemory(name=dsm_shm_name, create=False)
        dsm_arr = np.ndarray(dsm_shape, dtype=np.dtype(dsm_dtype), buffer=dsm_shm.buf)

    _worker_state = {
        'dtm_shm':  dtm_shm,
        'dtm_arr':  dtm_arr,
        'dtm_meta': dtm_meta,
        'dsm_shm':  dsm_shm,
        'dsm_arr':  dsm_arr,
        'dsm_meta': dsm_meta,
        'sensitivity_dbm': sensitivity_dbm,
    }


# ---------------------------------------------------------------------------
# Array-based helpers (run inside worker processes)
# ---------------------------------------------------------------------------

def _bilinear(arr: np.ndarray, meta: dict, lat: float, lon: float) -> Optional[float]:
    """Bilinear interpolation on a numpy array using array metadata."""
    if (lat < meta['lat_min'] or lat > meta['lat_max'] or
            lon < meta['lon_min'] or lon > meta['lon_max']):
        return None

    row = ((meta['lat_max'] - lat) /
           (meta['lat_max'] - meta['lat_min'])) * (meta['n_rows'] - 1)
    col = ((lon - meta['lon_min']) /
           (meta['lon_max'] - meta['lon_min'])) * (meta['n_cols'] - 1)

    r0 = max(0, min(int(row), meta['n_rows'] - 2))
    c0 = max(0, min(int(col), meta['n_cols'] - 2))
    fr, fc = row - r0, col - c0

    v = float(
        arr[r0,     c0    ] * (1 - fr) * (1 - fc) +
        arr[r0 + 1, c0    ] * fr       * (1 - fc) +
        arr[r0,     c0 + 1] * (1 - fr) * fc +
        arr[r0 + 1, c0 + 1] * fr       * fc
    )
    return None if (math.isnan(v) or v < -1000.0) else v


def _arr_elevation_dtm(lat: float, lon: float) -> Optional[float]:
    """DTM elevation at a point — always uses bare-earth model."""
    return _bilinear(
        _worker_state['dtm_arr'], _worker_state['dtm_meta'], lat, lon
    )


def _arr_elevation_surface(lat: float, lon: float) -> Optional[float]:
    """
    Surface elevation at a point (Change F).

    Returns DSM value when available, falls back to DTM.
    Used for profile obstacle heights — includes buildings and trees.
    """
    if _worker_state['dsm_arr'] is not None and _worker_state['dsm_meta'] is not None:
        v = _bilinear(
            _worker_state['dsm_arr'], _worker_state['dsm_meta'], lat, lon
        )
        if v is not None:
            return v
    # Fall back to bare-earth DTM
    return _arr_elevation_dtm(lat, lon)


def _arr_profile(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    num_points: int,
    use_dsm: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorised elevation profile from shared array(s).

    When use_dsm is True, obstacle heights come from the DSM (Change F);
    points not covered by the DSM fall back to DTM automatically via
    _arr_elevation_surface.

    Returns (distances_m, lats, elevations).
    """
    lats = np.linspace(lat1, lat2, num_points)
    lons = np.linspace(lon1, lon2, num_points)

    # Vectorised haversine distances from TX
    dlat = np.radians(lats - lat1)
    dlon = np.radians(lons - lon1)
    a = (np.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * np.cos(np.radians(lats)) *
         np.sin(dlon / 2) ** 2)
    distances_m = 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))

    if use_dsm and _worker_state['dsm_arr'] is not None:
        # Try to interpolate the whole profile from the DSM in one pass,
        # then patch any NaN cells with DTM values.
        dsm_meta = _worker_state['dsm_meta']
        dsm_arr  = _worker_state['dsm_arr']
        dtm_meta = _worker_state['dtm_meta']
        dtm_arr  = _worker_state['dtm_arr']

        elevations = _vec_bilinear(dsm_arr, dsm_meta, lats, lons)
        needs_dtm  = np.isnan(elevations) | (elevations < -1000.0)
        if needs_dtm.any():
            dtm_elev = _vec_bilinear(dtm_arr, dtm_meta, lats[needs_dtm], lons[needs_dtm])
            elevations[needs_dtm] = dtm_elev
    else:
        # DTM only
        dtm_meta = _worker_state['dtm_meta']
        dtm_arr  = _worker_state['dtm_arr']
        elevations = _vec_bilinear(dtm_arr, dtm_meta, lats, lons)

    elevations[elevations < -1000.0] = np.nan
    return distances_m, lats, elevations


def _vec_bilinear(
    arr: np.ndarray, meta: dict,
    lats: np.ndarray, lons: np.ndarray,
) -> np.ndarray:
    """
    Fully vectorised bilinear interpolation over arrays of lat/lon.
    Out-of-bounds points are returned as NaN.
    """
    n_rows = meta['n_rows']
    n_cols = meta['n_cols']
    lat_range = meta['lat_max'] - meta['lat_min']
    lon_range = meta['lon_max'] - meta['lon_min']

    rows = (meta['lat_max'] - lats) / lat_range * (n_rows - 1)
    cols = (lons - meta['lon_min']) / lon_range * (n_cols - 1)

    oob = (
        (lats < meta['lat_min']) | (lats > meta['lat_max']) |
        (lons < meta['lon_min']) | (lons > meta['lon_max'])
    )

    rows = np.clip(rows, 0, n_rows - 2)
    cols = np.clip(cols, 0, n_cols - 2)

    r0 = rows.astype(np.int32)
    c0 = cols.astype(np.int32)
    fr = rows - r0
    fc = cols - c0

    result = (
        arr[r0,     c0    ] * (1.0 - fr) * (1.0 - fc) +
        arr[r0 + 1, c0    ] * fr         * (1.0 - fc) +
        arr[r0,     c0 + 1] * (1.0 - fr) * fc +
        arr[r0 + 1, c0 + 1] * fr         * fc
    ).astype(np.float64)

    result[oob] = np.nan
    return result


# ---------------------------------------------------------------------------
# Worker function
# ---------------------------------------------------------------------------

def _compute_point(args: tuple) -> Optional[dict]:
    """
    Evaluates a single target grid point.

    Changes:
      Free-space pre-filter — rejects points where even perfect LOS
          cannot achieve link_margin > -20 dB.
      Uses shared-memory arrays via _arr_elevation_dtm /
          _arr_elevation_surface / _arr_profile.
      _arr_profile does a single vectorised pass over in-RAM arrays.
      Profile obstacle heights use DSM when available; TX/RX ground
          elevation always uses bare-earth DTM.
    """
    (
        target_lat, target_lon,
        ant_lat, ant_lon, ant_alt_m,
        rx_height_m, rx_gain_dbi,
        freq_mhz, modem_preset,
        tx_power_dbm, ant_gain_dbi,
        ant_azimuth, ant_beamwidth,
        ant_gain_min, ant_gain_max,
        has_dsm,
    ) = args

    try:
        dist_m = haversine_m(ant_lat, ant_lon, target_lat, target_lon)
        if dist_m < 10.0:
            return None

        # ── Free-space pre-filter ──────────────────────────────────────
        _fspl = fspl_db(dist_m, freq_mhz)
        _atm  = atmospheric_loss_db(dist_m, freq_mhz)
        sensitivity_dbm = _worker_state.get('sensitivity_dbm', -140.0)
        best_case_margin = (
            tx_power_dbm + ant_gain_dbi + rx_gain_dbi
            - _fspl - _atm - sensitivity_dbm
        )
        if best_case_margin < -20.0:
            return None

        brng = bearing_deg(ant_lat, ant_lon, target_lat, target_lon)

        if ant_beamwidth < 360.0:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            if diff > ant_beamwidth / 2.0:
                return None

        if ant_beamwidth >= 360.0:
            gain_used = ant_gain_dbi
        else:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            half_bw = ant_beamwidth / 2.0
            factor = max(0.0, 1.0 - diff / half_bw)
            gain_used = ant_gain_min + factor * (ant_gain_max - ant_gain_min)

        # ── RX ground elevation from DTM; surface elevation from DSM ───
        # The receiver is always at ground level + rx_height_m.
        # We use the bare-earth DTM for this so the 1.5 m height is above
        # real ground, not above a building roof or tree canopy.
        target_ground = _arr_elevation_dtm(target_lat, target_lon)
        if target_ground is None:
            return None
        rx_alt_m = target_ground + rx_height_m

        # Profile obstacle heights: DSM when available, DTM otherwise (F).
        n_samples = max(50, int(dist_m / 30))
        distances_m, lats, elevations = _arr_profile(
            ant_lat, ant_lon, target_lat, target_lon, n_samples,
            use_dsm=has_dsm,
        )

        # ── Earth bulge (vectorised) ────────────────────────────────────────
        bulge = np.fromiter(
            (earth_bulge_m(d) for d in distances_m), dtype=np.float64, count=n_samples
        )
        elevations_corr = np.where(np.isnan(elevations), np.nan, elevations + bulge)

        # ── LOS and Fresnel ────────────────────────────────────────────────
        los_ok, _ = check_los(
            distances_m, elevations_corr,
            ant_alt_m, rx_alt_m, dist_m,
            apply_earth_bulge=False,
        )
        if not los_ok:
            return {
                "lat": target_lat, "lon": target_lon,
                "distance_m": dist_m, "los": False,
                "fresnel_ok": False, "link_budget_db": float("-inf"),
                "bearing": brng,
            }

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
        }

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ViewshedParams:
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
# SharedMemory helpers
# ---------------------------------------------------------------------------

def _make_shm(arr: np.ndarray):
    """Copy a numpy array into a new SharedMemory block."""
    from multiprocessing.shared_memory import SharedMemory
    shm = SharedMemory(create=True, size=int(arr.nbytes))
    view = np.ndarray(arr.shape, dtype=arr.dtype, buffer=shm.buf)
    view[:] = arr[:]
    return shm, view


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
    from meshcoverage.processing.dem_handler import get_dem_handler, get_dsm_handler
    from meshcoverage.models.node import MODEM_PRESETS

    if n_workers <= 0:
        n_workers = max(1, os.cpu_count() or 1)

    log.info(
        f"Viewshed {params.node_id}: "
        f"range={params.max_range_m / 1000:.1f}km "
        f"res={params.resolution_m}m "
        f"workers={n_workers}"
    )

    # ── Bounding box with margin ──────────────────────────────────────────
    margin_deg = (params.max_range_m / 111_000.0) * 1.05
    margin_lon = margin_deg / max(0.01, math.cos(math.radians(params.ant_lat)))
    lat_min = params.ant_lat - margin_deg
    lat_max = params.ant_lat + margin_deg
    lon_min = params.ant_lon - margin_lon
    lon_max = params.ant_lon + margin_lon

    # ── Step 1: load DTM ────────────────────────────────────────────
    dem = get_dem_handler()
    log.info(f"Viewshed {params.node_id}: caricamento DTM in memoria...")
    dtm_arr, dtm_meta = dem.load_area_array(
        lat_min, lon_min, lat_max, lon_max,
        resolution_m=params.resolution_m,
    )
    if dtm_arr is None:
        log.error(f"Viewshed {params.node_id}: nessun dato DTM, impossibile calcolare")
        return []

    # ── Step 2: load DSM if available ────────────────────────────
    dsm_handler = get_dsm_handler()
    dsm_arr = dsm_meta = None
    has_dsm = False

    if dsm_handler is not None:
        log.info(f"Viewshed {params.node_id}: caricamento DSM in memoria...")
        dsm_arr, dsm_meta = dsm_handler.load_area_array(
            lat_min, lon_min, lat_max, lon_max,
            resolution_m=params.resolution_m,
        )
        if dsm_arr is not None:
            has_dsm = True
            log.info(
                f"Viewshed {params.node_id}: DSM disponibile — "
                f"ostacoli calcolati su superficie (edifici/vegetazione)"
            )
        else:
            log.warning(
                f"Viewshed {params.node_id}: DSM non copre l'area, "
                f"si usa solo il DTM bare-earth"
            )

    # ── Step 3: copy arrays into SharedMemory (A) ─────────────────────────
    dtm_shm, dtm_view = _make_shm(dtm_arr)
    del dtm_arr

    dsm_shm = dsm_view = None
    if dsm_arr is not None:
        dsm_shm, dsm_view = _make_shm(dsm_arr)
        del dsm_arr

    # Preset sensitivity for the free-space pre-filter
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
        dtm_shm.close(); dtm_shm.unlink()
        if dsm_shm:
            dsm_shm.close(); dsm_shm.unlink()
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
            has_dsm,
        )
        for lat, lon in targets
    ]

    results = []
    done    = 0
    total   = len(worker_args)

    # ── Step 4+5: worker pool ─────────────────────────────────────────
    try:
        with mp.Pool(
            processes=n_workers,
            initializer=_worker_init,
            initargs=(
                dtm_shm.name,
                dtm_view.shape,
                str(dtm_view.dtype),
                dtm_meta,
                dsm_shm.name if dsm_shm else "",
                dsm_view.shape if dsm_view is not None else (0,),
                str(dsm_view.dtype) if dsm_view is not None else "float32",
                dsm_meta,
                sensitivity_dbm,
            ),
        ) as pool:
            for result in pool.imap_unordered(
                _compute_point, worker_args, chunksize=50
            ):
                done += 1
                if result is not None:
                    results.append(result)
                if progress_callback and done % 100 == 0:
                    progress_callback(done, total)
    finally:
        # ── Step 6: always release SharedMemory ───────────────────────────
        dtm_shm.close()
        dtm_shm.unlink()
        if dsm_shm:
            dsm_shm.close()
            dsm_shm.unlink()

    if progress_callback:
        progress_callback(total, total)

    fresnel_ok_count = sum(1 for r in results if r.get("fresnel_ok"))
    dsm_note = " (con DSM clutter)" if has_dsm else " (DTM bare-earth)"
    log.info(
        f"Viewshed {params.node_id}{dsm_note}: "
        f"{len(results)} punti coperti ({fresnel_ok_count} con Fresnel ok)"
    )
    return results


# ---------------------------------------------------------------------------
# Persistence helpers (unchanged)
# ---------------------------------------------------------------------------

def save_viewshed(results: list[dict], output_path: Path, metadata: dict):
    if not results:
        np.savez_compressed(
            output_path,
            lats=np.array([]),
            lons=np.array([]),
            distances=np.array([]),
            los=np.array([], dtype=bool),
            fresnel_ok=np.array([], dtype=bool),
            link_budget=np.array([]),
            bearings=np.array([]),
            metadata=str(metadata),
        )
        return

    np.savez_compressed(
        output_path,
        lats=np.array([r["lat"]            for r in results]),
        lons=np.array([r["lon"]            for r in results]),
        distances=np.array([r["distance_m"]     for r in results]),
        los=np.array([r["los"]             for r in results], dtype=bool),
        fresnel_ok=np.array([r["fresnel_ok"]    for r in results], dtype=bool),
        link_budget=np.array([r["link_budget_db"] for r in results]),
        bearings=np.array([r["bearing"]        for r in results]),
        metadata=str(metadata),
    )
    log.info(f"Viewshed salvato: {output_path} ({len(results)} punti)")


def load_viewshed(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        data = np.load(path, allow_pickle=True)
        return {
            "lats":        data["lats"],
            "lons":        data["lons"],
            "distances":   data["distances"],
            "los":         data["los"],
            "fresnel_ok":  data["fresnel_ok"],
            "link_budget": data["link_budget"],
            "bearings":    data.get("bearings", np.array([])),
        }
    except Exception as e:
        log.error(f"Errore caricamento viewshed {path}: {e}")
        return None
