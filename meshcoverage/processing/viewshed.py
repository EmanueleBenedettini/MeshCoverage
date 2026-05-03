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

  Quad-tree adaptive grid — la griglia di punti target usa una struttura
      quad-tree per adattare la densità di campionamento alla variabilità
      del terreno (roughness locale). Terreni pianeggianti (laghi, pianure)
      rimangono a risoluzione grossolana; terreni accidentati (montagne,
      creste) vengono suddivisi fino alla risoluzione minima configurata.
      La roughness è calcolata su DTM + DSM (quando disponibile) per
      tenere conto anche di edifici e vegetazione come fonti di variabilità.

  Shadow zone persistence — i punti senza LOS (zone d'ombra) vengono ora
      salvati separatamente nel file NPZ, risolvendo il bug per cui
      shadow_lats/shadow_lons erano sempre vuoti nei dati caricati.
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
    # Preset sensitivity for pre-filter
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
    Surface elevation at a point.
    Returns DSM value when available, falls back to DTM.
    Used for profile obstacle heights — includes buildings and trees.
    """
    if _worker_state['dsm_arr'] is not None and _worker_state['dsm_meta'] is not None:
        v = _bilinear(
            _worker_state['dsm_arr'], _worker_state['dsm_meta'], lat, lon
        )
        if v is not None:
            return v
    return _arr_elevation_dtm(lat, lon)


def _arr_profile(
    lat1: float, lon1: float,
    lat2: float, lon2: float,
    num_points: int,
    use_dsm: bool,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorised elevation profile from shared array(s).

    When use_dsm is True, obstacle heights come from the DSM;
    points not covered by the DSM fall back to DTM automatically.

    Returns (distances_m, lats, elevations).
    """
    lats = np.linspace(lat1, lat2, num_points)
    lons = np.linspace(lon1, lon2, num_points)

    dlat = np.radians(lats - lat1)
    dlon = np.radians(lons - lon1)
    a = (np.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * np.cos(np.radians(lats)) *
         np.sin(dlon / 2) ** 2)
    distances_m = 2.0 * EARTH_RADIUS_M * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))

    if use_dsm and _worker_state['dsm_arr'] is not None:
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

    Uses shared-memory arrays via _arr_elevation_dtm /
    _arr_elevation_surface / _arr_profile.
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

        # ── RX ground elevation from DTM ───────────────────────────────
        # Always bare-earth so that the 1.5 m receiver height is above
        # real ground, not above a building or tree canopy.
        target_ground = _arr_elevation_dtm(target_lat, target_lon)
        if target_ground is None:
            return None
        rx_alt_m = target_ground + rx_height_m

        # ── Profile: obstacle heights from DSM when available ──────────
        n_samples = max(50, int(dist_m / 30))
        distances_m, lats, elevations = _arr_profile(
            ant_lat, ant_lon, target_lat, target_lon, n_samples,
            use_dsm=has_dsm,
        )

        # ── Earth bulge (vectorised) ────────────────────────────────────
        bulge = np.fromiter(
            (earth_bulge_m(d) for d in distances_m), dtype=np.float64, count=n_samples
        )
        elevations_corr = np.where(np.isnan(elevations), np.nan, elevations + bulge)

        # ── LOS and Fresnel ────────────────────────────────────────────
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
                "bearing": brng, "shadow": True,
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
            "bearing": brng, "shadow": False,
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
    resolution_m: float      # minimum (finest) resolution — used for DTM/DSM load
    rx_height_m: float


# ---------------------------------------------------------------------------
# Roughness computation (main process only, not in workers)
# ---------------------------------------------------------------------------

def _local_std_fast(arr: np.ndarray, window: int) -> np.ndarray:
    """
    Efficient local standard deviation via uniform_filter.

    Uses the identity: Var = E[X²] – E[X]²
    NaN cells are excluded from statistics via a valid-cell weight mask.
    """
    from scipy.ndimage import uniform_filter

    valid = (~np.isnan(arr)).astype(np.float32)
    arr_filled = np.where(np.isnan(arr), 0.0, arr).astype(np.float32)

    mode = 'reflect'
    valid_count = uniform_filter(valid, size=window, mode=mode)
    valid_count = np.maximum(valid_count, 1e-6)

    mean    = uniform_filter(arr_filled * valid,        size=window, mode=mode) / valid_count
    mean_sq = uniform_filter(arr_filled ** 2 * valid,   size=window, mode=mode) / valid_count
    variance = np.maximum(mean_sq - mean ** 2, 0.0)
    return np.sqrt(variance)


def compute_roughness_map(
    dtm_arr: np.ndarray,
    dtm_meta: dict,
    dsm_arr: Optional[np.ndarray] = None,
    dsm_meta: Optional[dict] = None,
    resolution_m: float = 100.0,
) -> tuple[np.ndarray, dict]:
    """
    Compute a terrain roughness map (local elevation σ) from DTM and optional DSM.

    The roughness drives quad-tree cell subdivision: high σ → fine grid,
    low σ (flat terrain, water) → coarse grid.

    When DSM is present, the roughness is the component-wise maximum of:
      - DTM roughness      (bare-earth terrain variation)
      - DSM roughness      (surface variation, incl. buildings/trees)
      - |DSM – DTM| × 0.5  (clutter height contribution to variation)

    The |DSM – DTM| term ensures urban canyons and dense forest (where the
    DTM is smooth but the DSM has abrupt high values) still trigger fine
    subdivision, improving profile accuracy for DSM-obstructed paths.

    Args:
        dtm_arr:       Bare-earth DTM array loaded by load_area_array()
        dtm_meta:      DTM spatial metadata dict
        dsm_arr:       Optional DSM array (same resolution recommended)
        dsm_meta:      DSM spatial metadata dict
        resolution_m:  Cell size of the arrays (used to choose window size)

    Returns:
        (roughness_arr, roughness_meta)
        roughness_meta is identical to dtm_meta (same spatial extent).
    """
    # Window ≈ 700 m physical footprint, clamped to [3, 11] cells
    window = max(3, min(11, round(700.0 / max(resolution_m, 1.0))))

    dtm_roughness = _local_std_fast(dtm_arr, window)

    if dsm_arr is not None and dsm_meta is not None:
        if dsm_arr.shape == dtm_arr.shape:
            dsm_roughness = _local_std_fast(dsm_arr, window)

            # Clutter contribution: half of the surface-to-ground height difference
            dsm_dtm_diff = np.where(
                np.isnan(dsm_arr) | np.isnan(dtm_arr),
                0.0,
                np.abs(dsm_arr.astype(np.float64) - dtm_arr.astype(np.float64)),
            )
            clutter_roughness = (dsm_dtm_diff * 0.5).astype(np.float32)

            roughness = np.maximum(dtm_roughness,
                        np.maximum(dsm_roughness, clutter_roughness))
        else:
            # Different array shapes (e.g. different source resolutions that
            # happened to produce slightly different grid sizes after reproject).
            # Resampling here would add complexity; fall back to DTM-only.
            log.warning(
                "compute_roughness_map: DTM shape %s ≠ DSM shape %s — "
                "using DTM-only roughness for quad-tree subdivision.",
                dtm_arr.shape, dsm_arr.shape,
            )
            roughness = dtm_roughness
    else:
        roughness = dtm_roughness

    return roughness, dict(dtm_meta)


# ---------------------------------------------------------------------------
# Quad-tree grid generation
# ---------------------------------------------------------------------------

class _QuadCell:
    """A rectangular cell in lat/lon space, used during quad-tree traversal."""
    __slots__ = ('lat_min', 'lon_min', 'lat_max', 'lon_max')

    def __init__(
        self,
        lat_min: float, lon_min: float,
        lat_max: float, lon_max: float,
    ):
        self.lat_min = lat_min
        self.lon_min = lon_min
        self.lat_max = lat_max
        self.lon_max = lon_max

    @property
    def center(self) -> tuple[float, float]:
        return (
            (self.lat_min + self.lat_max) / 2.0,
            (self.lon_min + self.lon_max) / 2.0,
        )

    def size_m(self) -> float:
        """Shorter physical dimension of the cell in metres."""
        clat, clon = self.center
        h = haversine_m(self.lat_min, clon,  self.lat_max, clon)
        w = haversine_m(clat, self.lon_min,  clat, self.lon_max)
        return min(h, w)

    def subdivide(self) -> list['_QuadCell']:
        """Split into four equal child cells."""
        lat_mid = (self.lat_min + self.lat_max) / 2.0
        lon_mid = (self.lon_min + self.lon_max) / 2.0
        return [
            _QuadCell(self.lat_min, self.lon_min, lat_mid,       lon_mid),
            _QuadCell(self.lat_min, lon_mid,      lat_mid,       self.lon_max),
            _QuadCell(lat_mid,      self.lon_min, self.lat_max,  lon_mid),
            _QuadCell(lat_mid,      lon_mid,      self.lat_max,  self.lon_max),
        ]


def _roughness_at(
    roughness_arr: np.ndarray,
    roughness_meta: dict,
    lat: float,
    lon: float,
) -> float:
    """
    Bilinear interpolation of roughness at a (lat, lon) point.
    Returns 0.0 (flat) when the point is outside the roughness array extent.
    """
    m = roughness_meta
    if (lat < m['lat_min'] or lat > m['lat_max'] or
            lon < m['lon_min'] or lon > m['lon_max']):
        return 0.0

    row = ((m['lat_max'] - lat) /
           (m['lat_max'] - m['lat_min'])) * (m['n_rows'] - 1)
    col = ((lon - m['lon_min']) /
           (m['lon_max'] - m['lon_min'])) * (m['n_cols'] - 1)

    r0 = max(0, min(int(row), m['n_rows'] - 2))
    c0 = max(0, min(int(col), m['n_cols'] - 2))
    fr, fc = row - r0, col - c0

    v = (roughness_arr[r0,     c0    ] * (1 - fr) * (1 - fc) +
         roughness_arr[r0 + 1, c0    ] * fr       * (1 - fc) +
         roughness_arr[r0,     c0 + 1] * (1 - fr) * fc +
         roughness_arr[r0 + 1, c0 + 1] * fr       * fc)
    return float(v)


def _should_subdivide(
    roughness_m: float,
    cell_size_m: float,
    min_resolution_m: float,
) -> bool:
    """
    Return True when a quad cell should be split into four children.

    Subdivision stops unconditionally once cell_size ≤ min_resolution_m.
    Above that floor, the roughness threshold determines the target
    resolution level:

      σ > 80 m  (alpine ridges, cliffs, dense urban)  → min_resolution_m
      σ > 20 m  (hilly, mixed woodland)                → 2 × min_resolution_m
      σ >  5 m  (gentle slopes, suburban)              → 4 × min_resolution_m
      σ ≤  5 m  (plains, lakes, sea)                   → 8 × min_resolution_m
                                                          (= max cell, no split)
    """
    # Hard floor: never go below the configured minimum resolution
    if cell_size_m <= min_resolution_m * 1.05:
        return False

    if roughness_m > 80.0:
        # Very rough: subdivide until at the minimum resolution
        return True
    elif roughness_m > 20.0:
        return cell_size_m > min_resolution_m * 2.0
    elif roughness_m > 5.0:
        return cell_size_m > min_resolution_m * 4.0
    else:
        # Flat terrain: stay at coarsest level (8 × min_resolution_m)
        return False


def generate_quadtree_grid(
    center_lat: float,
    center_lon: float,
    max_range_m: float,
    min_resolution_m: float,
    roughness_arr: np.ndarray,
    roughness_meta: dict,
    ant_azimuth: float = 0.0,
    ant_beamwidth: float = 360.0,
) -> list[tuple[float, float]]:
    """
    Generate a variable-density target point grid using quad-tree subdivision.

    Starting from cells of size 8 × min_resolution_m (capped at 400 m),
    each cell is recursively split into four children whenever the local
    terrain roughness warrants it (see _should_subdivide).

    Flat terrain (lakes, plains) produces very few points; rough terrain
    (mountain ridges, urban areas) is sampled at full min_resolution_m
    density.  The total point count is thus driven by terrain complexity
    rather than by a fixed uniform grid.

    Roughness is queried at the centre of each candidate cell.  For DSM-
    augmented roughness (which includes building/tree clutter), urban areas
    and forests automatically receive finer sampling.

    Args:
        center_lat, center_lon: Antenna position
        max_range_m:            Analysis radius in metres
        min_resolution_m:       Finest allowed cell size (from config)
        roughness_arr:          Terrain roughness array (σ in metres)
        roughness_meta:         Spatial metadata for roughness_arr
        ant_azimuth:            Antenna pointing direction (degrees)
        ant_beamwidth:          Antenna beamwidth (degrees; 360 = omni)

    Returns:
        List of (lat, lon) tuples — one point per leaf quad-tree cell.
    """
    # Coarsest cell: 8 × min, capped at 400 m.
    # This gives 3 subdivision levels (8→4→2→1 × min).
    max_resolution_m = min(400.0, min_resolution_m * 8.0)
    # Ensure at least 2 levels are possible even for coarse configs
    max_resolution_m = max(max_resolution_m, min_resolution_m * 2.0)

    # Bounding box in degrees
    margin_deg = max_range_m / 111_000.0
    cos_lat = max(0.01, math.cos(math.radians(center_lat)))
    margin_lon = margin_deg / cos_lat

    lat_min_bb = center_lat - margin_deg
    lat_max_bb = center_lat + margin_deg
    lon_min_bb = center_lon - margin_lon
    lon_max_bb = center_lon + margin_lon

    # Seed the stack with the initial coarse grid
    lat_step = max_resolution_m / 111_000.0
    lon_step = max_resolution_m / (111_000.0 * cos_lat)

    stack: list[_QuadCell] = []
    lat = lat_min_bb
    while lat < lat_max_bb:
        lon = lon_min_bb
        while lon < lon_max_bb:
            stack.append(_QuadCell(
                lat, lon,
                min(lat + lat_step, lat_max_bb),
                min(lon + lon_step, lon_max_bb),
            ))
            lon += lon_step
        lat += lat_step

    log.debug(
        "generate_quadtree_grid: %d seed cells at %.0f m, "
        "range=%.1f km, min_res=%.0f m",
        len(stack), max_resolution_m, max_range_m / 1000.0, min_resolution_m,
    )

    leaf_points: list[tuple[float, float]] = []
    half_min = min_resolution_m / 2.0

    while stack:
        cell = stack.pop()
        clat, clon = cell.center

        # Distance from antenna to cell centre
        dist = haversine_m(center_lat, center_lon, clat, clon)

        # Conservative range check: keep cells that could overlap the analysis area.
        # cell.size_m() * √2 ≈ cell diagonal used as safety margin.
        cell_size = cell.size_m()
        cell_diag = cell_size * 1.5
        if dist > max_range_m + cell_diag:
            continue

        # Antenna sector pre-filter for directional antennas.
        # Add a margin proportional to the angular size of the cell at this
        # distance so cells straddling the sector edge are not discarded.
        if ant_beamwidth < 360.0 and dist > 100.0:
            brng = bearing_deg(center_lat, center_lon, clat, clon)
            diff = abs(((brng - ant_azimuth) + 180) % 360 - 180)
            # Angular half-width of the cell at this distance (radians → degrees)
            angular_margin = math.degrees(math.atan2(cell_size / 2.0, max(dist, 1.0)))
            if diff > ant_beamwidth / 2.0 + angular_margin + 5.0:
                continue

        # Query roughness at cell centre
        roughness = _roughness_at(roughness_arr, roughness_meta, clat, clon)

        if _should_subdivide(roughness, cell_size, min_resolution_m):
            stack.extend(cell.subdivide())
        else:
            # Leaf: emit centre point if within range and not too close to TX
            if dist <= max_range_m and dist >= half_min:
                leaf_points.append((clat, clon))

    return leaf_points


# ---------------------------------------------------------------------------
# Legacy uniform grid (kept as fallback)
# ---------------------------------------------------------------------------

def generate_target_grid(
    center_lat: float, center_lon: float,
    max_range_m: float, resolution_m: float,
    ant_azimuth: float = 0.0, ant_beamwidth: float = 360.0,
) -> list[tuple[float, float]]:
    """
    Uniform Cartesian grid (legacy fallback).
    Prefer generate_quadtree_grid() for quality-oriented computations.
    """
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
    Calcola il viewshed completo per un nodo con griglia adattiva quad-tree.

    Flusso:
      1. Carica DTM in memoria (SharedMemory) a min_resolution_m
      2. Carica DSM se configurato (SharedMemory) a min_resolution_m
      3. Calcola roughness map da DTM + DSM (processo principale)
      4. Genera griglia quad-tree adattiva basata su roughness
      5. Esegue worker pool — ogni punto elaborato in parallelo
      6. Libera SharedMemory
      7. Restituisce risultati (coperti + shadow zone)

    Args:
        params:            Parametri del calcolo (antenna, frequenza, range, ecc.)
        n_workers:         Processi paralleli (0 = usa tutti i core)
        progress_callback: callable(done, total) per aggiornamento progresso

    Returns:
        Lista di dict con chiavi: lat, lon, distance_m, los, fresnel_ok,
        link_budget_db, bearing, shadow.
    """
    from multiprocessing.shared_memory import SharedMemory
    from meshcoverage.processing.dem_handler import get_dem_handler, get_dsm_handler
    from meshcoverage.models.node import MODEM_PRESETS

    if n_workers <= 0:
        n_workers = max(1, os.cpu_count() or 1)

    log.info(
        "Viewshed %s: range=%.1f km  min_res=%d m  workers=%d",
        params.node_id, params.max_range_m / 1000.0,
        int(params.resolution_m), n_workers,
    )

    # ── Bounding box with 5 % margin ──────────────────────────────────────
    margin_deg = (params.max_range_m / 111_000.0) * 1.05
    margin_lon = margin_deg / max(0.01, math.cos(math.radians(params.ant_lat)))
    lat_min = params.ant_lat - margin_deg
    lat_max = params.ant_lat + margin_deg
    lon_min = params.ant_lon - margin_lon
    lon_max = params.ant_lon + margin_lon

    # ── Step 1: load DTM ──────────────────────────────────────────────────
    dem = get_dem_handler()
    log.info("Viewshed %s: loading DTM into RAM...", params.node_id)
    dtm_arr, dtm_meta = dem.load_area_array(
        lat_min, lon_min, lat_max, lon_max,
        resolution_m=params.resolution_m,
    )
    if dtm_arr is None:
        log.error("Viewshed %s: no DTM data — cannot compute", params.node_id)
        return []

    # ── Step 2: load DSM if available ─────────────────────────────────────
    dsm_handler = get_dsm_handler()
    dsm_arr = dsm_meta = None
    has_dsm = False

    if dsm_handler is not None:
        log.info("Viewshed %s: loading DSM into RAM...", params.node_id)
        dsm_arr, dsm_meta = dsm_handler.load_area_array(
            lat_min, lon_min, lat_max, lon_max,
            resolution_m=params.resolution_m,
        )
        if dsm_arr is not None:
            has_dsm = True
            log.info(
                "Viewshed %s: DSM loaded — obstacle heights from surface model "
                "(buildings/vegetation included)",
                params.node_id,
            )
        else:
            log.warning(
                "Viewshed %s: DSM configured but does not cover this area — "
                "falling back to bare-earth DTM only",
                params.node_id,
            )

    # ── Step 3: compute roughness map ─────────────────────────────────────
    # Uses both DTM and DSM (when available) so that urban/forest clutter
    # also drives fine subdivision, not only bare-earth terrain variation.
    log.info("Viewshed %s: computing roughness map%s...",
             params.node_id, " (DTM + DSM)" if has_dsm else " (DTM only)")
    try:
        roughness_arr, roughness_meta = compute_roughness_map(
            dtm_arr, dtm_meta,
            dsm_arr if has_dsm else None,
            dsm_meta if has_dsm else None,
            resolution_m=params.resolution_m,
        )
        roughness_stats = {
            "min": float(np.nanmin(roughness_arr)),
            "max": float(np.nanmax(roughness_arr)),
            "mean": float(np.nanmean(roughness_arr)),
            "p95": float(np.nanpercentile(roughness_arr, 95)),
        }
        log.info(
            "Viewshed %s: roughness σ  min=%.1f m  mean=%.1f m  "
            "p95=%.1f m  max=%.1f m",
            params.node_id,
            roughness_stats["min"], roughness_stats["mean"],
            roughness_stats["p95"], roughness_stats["max"],
        )
    except Exception as exc:
        log.warning(
            "Viewshed %s: roughness computation failed (%s) — "
            "falling back to uniform grid at min_resolution_m",
            params.node_id, exc,
        )
        roughness_arr = None
        roughness_meta = None

    # ── Step 4: generate quad-tree (or fallback uniform) grid ─────────────
    if roughness_arr is not None:
        targets = generate_quadtree_grid(
            params.ant_lat, params.ant_lon,
            params.max_range_m, params.resolution_m,
            roughness_arr, roughness_meta,
            params.ant_azimuth_deg, params.ant_beamwidth_deg,
        )
        grid_mode = "quad-tree"
    else:
        targets = generate_target_grid(
            params.ant_lat, params.ant_lon,
            params.max_range_m, params.resolution_m,
            params.ant_azimuth_deg, params.ant_beamwidth_deg,
        )
        grid_mode = "uniform (fallback)"

    log.info(
        "Viewshed %s: %d target points  mode=%s  min_res=%d m",
        params.node_id, len(targets), grid_mode, int(params.resolution_m),
    )

    if not targets:
        return []

    # ── Step 5: copy arrays into SharedMemory ─────────────────────────────
    dtm_shm, dtm_view = _make_shm(dtm_arr)
    del dtm_arr

    dsm_shm = dsm_view = None
    if dsm_arr is not None:
        dsm_shm, dsm_view = _make_shm(dsm_arr)
        del dsm_arr

    preset_data     = MODEM_PRESETS.get(params.modem_preset, {})
    sensitivity_dbm = preset_data.get("receiver_sensitivity_dbm", -140.0)

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

    # ── Step 6: worker pool ───────────────────────────────────────────────
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
        # ── Step 7: always release SharedMemory ───────────────────────────
        dtm_shm.close()
        dtm_shm.unlink()
        if dsm_shm:
            dsm_shm.close()
            dsm_shm.unlink()

    if progress_callback:
        progress_callback(total, total)

    covered_count = sum(1 for r in results if r.get("los"))
    shadow_count  = sum(1 for r in results if r.get("shadow"))
    fresnel_count = sum(1 for r in results if r.get("fresnel_ok"))
    dsm_note = " (DTM + DSM clutter)" if has_dsm else " (DTM bare-earth)"

    log.info(
        "Viewshed %s%s: %d covered  %d shadow  %d Fresnel-ok  "
        "(%d total evaluated)",
        params.node_id, dsm_note,
        covered_count, shadow_count, fresnel_count, len(results),
    )
    return results


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def save_viewshed(results: list[dict], output_path: Path, metadata: dict):
    """
    Persist viewshed results to a compressed NPZ file.

    Coverage points (los=True) and shadow zone points (los=False) are stored
    in separate arrays so that downstream consumers (heatmap generator,
    shadow overlay API) can access them independently without re-filtering.
    """
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
            shadow_lats=np.array([]),
            shadow_lons=np.array([]),
            shadow_distances=np.array([]),
            metadata=str(metadata),
        )
        return

    # Partition results into coverage points and shadow zone points
    covered = [r for r in results if r.get("los", False)]
    shadows = [r for r in results if not r.get("los", False)]

    np.savez_compressed(
        output_path,
        # ── Coverage points ──────────────────────────────────────────────
        lats=np.array([r["lat"]             for r in covered]),
        lons=np.array([r["lon"]             for r in covered]),
        distances=np.array([r["distance_m"]      for r in covered]),
        los=np.array([r["los"]              for r in covered], dtype=bool),
        fresnel_ok=np.array([r["fresnel_ok"]     for r in covered], dtype=bool),
        link_budget=np.array([r["link_budget_db"] for r in covered]),
        bearings=np.array([r["bearing"]         for r in covered]),
        # ── Shadow zone points ───────────────────────────────────────────
        shadow_lats=np.array([r["lat"]        for r in shadows]),
        shadow_lons=np.array([r["lon"]        for r in shadows]),
        shadow_distances=np.array([r["distance_m"] for r in shadows]),
        metadata=str(metadata),
    )
    log.info(
        "Viewshed saved: %s  (%d coverage, %d shadow points)",
        output_path, len(covered), len(shadows),
    )


def load_viewshed(path: Path) -> Optional[dict]:
    """
    Load a previously saved viewshed NPZ file.

    Returns a dict with both coverage arrays and shadow zone arrays.
    Shadow arrays default to empty if the file pre-dates shadow persistence
    (i.e. was saved by an older version of save_viewshed).
    """
    if not path.exists():
        return None
    try:
        data = np.load(path, allow_pickle=True)
        files = data.files  # list of array names in the npz

        return {
            # Coverage arrays (always present)
            "lats":             data["lats"],
            "lons":             data["lons"],
            "distances":        data["distances"],
            "los":              data["los"],
            "fresnel_ok":       data["fresnel_ok"],
            "link_budget":      data["link_budget"],
            "bearings":         data["bearings"] if "bearings" in files else np.array([]),
            # Shadow zone arrays (present only in files saved by the new code)
            "shadow_lats":      data["shadow_lats"]      if "shadow_lats"      in files else np.array([]),
            "shadow_lons":      data["shadow_lons"]      if "shadow_lons"      in files else np.array([]),
            "shadow_distances": data["shadow_distances"] if "shadow_distances" in files else np.array([]),
        }
    except Exception as e:
        log.error("Error loading viewshed %s: %s", path, e)
        return None
