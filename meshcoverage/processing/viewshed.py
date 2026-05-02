"""
Calcolo viewshed (analisi di visibilità) parallelizzato.

Algoritmo:
1. Genera una griglia di punti target nell'area di interesse
2. Per ogni punto target, estrae il profilo di elevazione DEM
3. Verifica LOS e clearance zona di Fresnel
4. Calcola link budget per i punti raggiungibili
5. Salva risultati in formato compresso (.npz)
   — include anche le shadow zone (punti senza LOS)

La parallelizzazione usa multiprocessing per sfruttare tutti i core disponibili.
NON usa settori angolari fissi (che creano buchi a lunga distanza),
ma itera su tutti i punti della griglia DEM nell'area di interesse.
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
    DEMHandler, haversine_m, bearing_deg, earth_bulge_m, destination_point
)
from meshcoverage.processing.fresnel import check_los, check_fresnel_clearance
from meshcoverage.processing.link_budget import calculate_link_budget

log = logging.getLogger(__name__)


@dataclass
class ViewshedParams:
    """Parametri per il calcolo viewshed di un singolo nodo."""
    node_id: str
    ant_lat: float
    ant_lon: float
    ant_alt_m: float          # Altitudine assoluta antenna (m slm)
    ground_height_m: float    # Altezza antenna dal suolo (m)
    freq_mhz: int
    modem_preset: str
    tx_power_dbm: float
    ant_gain_dbi: float
    ant_azimuth_deg: float
    ant_beamwidth_deg: float
    ant_gain_min_dbi: float
    ant_gain_max_dbi: float
    rx_gain_dbi: float        # Guadagno antenna ricevente (default 2.15)
    max_range_m: float        # Distanza massima da analizzare
    dem_dir: Path
    resolution_m: float       # Risoluzione griglia punti target
    rx_height_m: float        # Altezza ricevitore sopra il suolo (default 1.5m)


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


def _compute_point(args: tuple) -> Optional[dict]:
    """
    Funzione worker per il calcolo di un singolo punto target.
    Deve essere top-level per funzionare con multiprocessing.

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
        dem_dir_str,
    ) = args

    try:
        from meshcoverage.processing.dem_handler import DEMHandler, haversine_m
        from meshcoverage.processing.fresnel import check_los, check_fresnel_clearance
        from meshcoverage.processing.link_budget import calculate_link_budget

        dem = DEMHandler(Path(dem_dir_str))
        dem.initialize()

        # Distanza totale TX → target
        dist_m = haversine_m(ant_lat, ant_lon, target_lat, target_lon)
        if dist_m < 10:
            return None

        # Bearing TX → target
        brng = bearing_deg(ant_lat, ant_lon, target_lat, target_lon)

        # Verifica angolo di copertura antenna
        if ant_beamwidth < 360.0:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            if diff > ant_beamwidth / 2.0:
                return None  # Fuori dal fascio — non è shadow zone, è semplicemente fuori settore

        # Calcola guadagno antenna per questo angolo
        if ant_beamwidth >= 360.0:
            gain_used = ant_gain_dbi
        else:
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            half_bw = ant_beamwidth / 2.0
            factor = max(0.0, 1.0 - diff / half_bw)
            gain_used = ant_gain_min + factor * (ant_gain_max - ant_gain_min)

        # Altitudine terreno al punto target
        target_elev = dem.get_elevation(target_lat, target_lon)
        if target_elev is None:
            return None  # Nessun dato DEM — non possiamo determinare se shadow

        rx_alt_m = target_elev + rx_height_m  # Altitudine assoluta ricevitore

        # Profilo di elevazione TX → target
        n_samples = max(50, int(dist_m / 30))  # campione ogni ~30m
        distances_m, lats, elevations = dem.get_profile(
            ant_lat, ant_lon, target_lat, target_lon, n_samples
        )

        # Aggiungi rigonfiamento terrestre alle elevazioni
        elevations_corr = np.where(
            np.isnan(elevations),
            np.nan,
            elevations + np.array([earth_bulge_m(d) for d in distances_m])
        )

        # 1. Check LOS puro
        los_ok, los_clearance = check_los(
            distances_m, elevations_corr,
            ant_alt_m, rx_alt_m, dist_m,
            apply_earth_bulge=False  # già applicato sopra
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

        # 2. Check zona di Fresnel
        fresnel_ok, fresnel_clearance = check_fresnel_clearance(
            distances_m, elevations_corr,
            ant_alt_m, rx_alt_m, dist_m, freq_mhz
        )

        # 3. Link budget (anche senza Fresnel ok, ma con penalità)
        diffraction_loss = 0.0 if fresnel_ok else 6.0  # penalità 6dB senza Fresnel

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

    except Exception as e:
        return None


def generate_target_grid(
    center_lat: float, center_lon: float,
    max_range_m: float, resolution_m: float,
    ant_azimuth: float = 0.0, ant_beamwidth: float = 360.0,
) -> list[tuple[float, float]]:
    """
    Genera la griglia di punti target nell'area di interesse.
    
    Itera su tutti i punti della griglia (non per settori angolari)
    per evitare buchi nelle lunghe distanze.
    Filtra in base al settore dell'antenna se direzionale.
    """
    # Dimensioni griglia in gradi
    lat_step = resolution_m / 111_000.0
    lon_step = resolution_m / (111_000.0 * math.cos(math.radians(center_lat)))

    # Quanti step in ogni direzione
    n_steps = int(max_range_m / resolution_m) + 1

    points = []
    for i in range(-n_steps, n_steps + 1):
        for j in range(-n_steps, n_steps + 1):
            lat = center_lat + i * lat_step
            lon = center_lon + j * lon_step

            # Check distanza massima
            dist = haversine_m(center_lat, center_lon, lat, lon)
            if dist > max_range_m or dist < resolution_m / 2:
                continue

            # Check settore antenna se direzionale
            if ant_beamwidth < 360.0:
                brng = bearing_deg(center_lat, center_lon, lat, lon)
                diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
                if diff > ant_beamwidth / 2.0 + 10:  # margine 10°
                    continue

            points.append((lat, lon))

    return points


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
    if n_workers <= 0:
        n_workers = max(1, os.cpu_count() or 1)

    log.info(
        f"Viewshed {params.node_id}: "
        f"range={params.max_range_m/1000:.1f}km "
        f"res={params.resolution_m}m "
        f"workers={n_workers}"
    )

    # Genera griglia target
    targets = generate_target_grid(
        params.ant_lat, params.ant_lon,
        params.max_range_m, params.resolution_m,
        params.ant_azimuth_deg, params.ant_beamwidth_deg,
    )

    log.info(f"Viewshed {params.node_id}: {len(targets)} punti target da analizzare")

    if not targets:
        return []

    # Costruisci args per ogni worker
    ant_gain_min = params.ant_gain_min_dbi if params.ant_gain_min_dbi is not None else params.ant_gain_dbi - 3
    ant_gain_max = params.ant_gain_max_dbi if params.ant_gain_max_dbi is not None else params.ant_gain_dbi

    worker_args = [
        (
            lat, lon,
            params.ant_lat, params.ant_lon, params.ant_alt_m,
            params.rx_height_m, params.rx_gain_dbi,
            params.freq_mhz, params.modem_preset,
            params.tx_power_dbm, params.ant_gain_dbi,
            params.ant_azimuth_deg, params.ant_beamwidth_deg,
            ant_gain_min, ant_gain_max,
            str(params.dem_dir),
        )
        for lat, lon in targets
    ]

    results = []
    done = 0
    total = len(worker_args)

    # Parallelizzazione con multiprocessing
    with mp.Pool(processes=n_workers) as pool:
        for result in pool.imap_unordered(_compute_point, worker_args, chunksize=20):
            done += 1
            if result is not None:
                results.append(result)
            if progress_callback and done % 100 == 0:
                progress_callback(done, total)

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
