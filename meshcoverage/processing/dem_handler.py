"""
Gestione file DEM/DSM (Digital Elevation/Surface Model) in formato GeoTIFF.
Supporta file multipli affiancati, indicizzati automaticamente.

  Two independent handler singletons are provided:
    get_dem_handler() → bare-earth DTM  (always required)
    get_dsm_handler() → surface model   (optional; returns None when
                         MESHCOVERAGE_DSM_DIR is not set or empty)

  The DSM is used by the viewshed algorithm for obstacle heights along the
  propagation path (buildings, trees).  TX and RX ground elevations always
  come from the bare-earth DTM so antenna height above ground and the 1.5 m
  receiver height are measured correctly.

  Recommended DSM sources (GeoTIFF, place tiles in dsm_dir):
    - Copernicus DEM GLO-30 DSM (free, global, 30 m):
        https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model
    - ALOS AW3D30 (free, global, 30 m):
        https://www.eorc.jaxa.jp/ALOS/en/aw3d30/
    - Local LiDAR DSM from national mapping agencies (best accuracy).
"""
from __future__ import annotations
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import rasterio
    from rasterio.transform import rowcol
    from pyproj import Transformer
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False
    log.warning("rasterio non disponibile. Funzionalità DEM/DSM disabilitate.")


class DEMHandler:
    """
    Generic handler for DEM or DSM GeoTIFF files.
    Loads and indexes all files in a directory.
    Provides elevation queries by lat/lon and bulk area loading.
    """

    def __init__(self, dem_dir: Path):
        self.dem_dir = dem_dir
        self._datasets: list = []
        self._bounds: list[tuple] = []
        self._resolutions: list[float] = []
        self._initialized = False

    def initialize(self):
        """Scan directory and index all GeoTIFF files."""
        if not HAS_RASTERIO:
            log.error("rasterio non disponibile, impossibile usare DEM/DSM")
            return

        self._datasets = []
        self._bounds = []
        self._resolutions = []

        tif_files = (
            sorted(self.dem_dir.glob("*.tif")) +
            sorted(self.dem_dir.glob("*.tiff"))
        )
        if not tif_files:
            log.warning(f"Nessun file trovato in {self.dem_dir}")
            return

        log.info(f"Trovati {len(tif_files)} file in {self.dem_dir}")

        dem_entries = []
        for f in tif_files:
            try:
                ds = rasterio.open(f)
                if ds.crs and ds.crs.to_epsg() != 4326:
                    transformer = Transformer.from_crs(
                        ds.crs, "EPSG:4326", always_xy=True
                    )
                    left, top = transformer.transform(ds.bounds.left, ds.bounds.top)
                    right, bottom = transformer.transform(
                        ds.bounds.right, ds.bounds.bottom
                    )
                    bounds_ll = (
                        min(left, right), min(top, bottom),
                        max(left, right), max(top, bottom),
                    )
                else:
                    b = ds.bounds
                    bounds_ll = (b.left, b.bottom, b.right, b.top)

                resolution_m = (abs(ds.transform[0]) + abs(ds.transform[4])) / 2.0
                dem_entries.append((ds, bounds_ll, resolution_m, f.name))
                log.debug(
                    f"File caricato: {f.name} "
                    f"bounds={bounds_ll} res={resolution_m:.2f}m"
                )
            except Exception as e:
                log.error(f"Errore apertura {f}: {e}")

        # Sort ascending by resolution so highest-res data comes first in the list
        dem_entries.sort(key=lambda x: x[2])

        for ds, bounds, res, _ in dem_entries:
            self._datasets.append(ds)
            self._bounds.append(bounds)
            self._resolutions.append(res)

        self._initialized = True
        log.info(
            f"Inizializzati {len(self._datasets)} file in {self.dem_dir} "
            f"(ordinati per risoluzione)"
        )

    # ------------------------------------------------------------------
    # Bulk area load — single I/O pass per dataset (Change C)
    # ------------------------------------------------------------------

    def load_area_array(
        self,
        lat_min: float, lon_min: float,
        lat_max: float, lon_max: float,
        resolution_m: float = 30.0,
    ) -> tuple[Optional[np.ndarray], Optional[dict]]:
        """
        Load elevation data for a bounding box into a float32 numpy array
        using a single windowed reproject per dataset (Change C).

        Array layout:
          row 0   → lat_max (north)   col 0   → lon_min (west)
          row n-1 → lat_min (south)   col m-1 → lon_max (east)

        Nodata / sub-ocean values stored as NaN.
        Returns (None, None) when no dataset covers the area.
        """
        if not HAS_RASTERIO:
            return None, None
        if not self._initialized or not self._datasets:
            return None, None

        relevant = [
            ds for ds, bounds in zip(self._datasets, self._bounds)
            if (bounds[0] <= lon_max and bounds[2] >= lon_min and
                bounds[1] <= lat_max and bounds[3] >= lat_min)
        ]
        if not relevant:
            log.warning(
                f"load_area_array ({self.dem_dir.name}): nessun dataset copre "
                f"[{lat_min:.3f},{lon_min:.3f}]→[{lat_max:.3f},{lon_max:.3f}]"
            )
            return None, None

        lat_res = resolution_m / 111_000.0
        lon_res = resolution_m / (
            111_000.0 * math.cos(math.radians((lat_min + lat_max) / 2))
        )
        n_rows = max(4, int((lat_max - lat_min) / lat_res) + 2)
        n_cols = max(4, int((lon_max - lon_min) / lon_res) + 2)

        result = np.full((n_rows, n_cols), np.nan, dtype=np.float32)

        from rasterio.transform import from_bounds as rasterio_from_bounds
        from rasterio.warp import reproject, Resampling
        from rasterio.crs import CRS

        target_transform = rasterio_from_bounds(
            lon_min, lat_min, lon_max, lat_max, n_cols, n_rows
        )
        target_crs = CRS.from_epsg(4326)

        # Process lowest-res → highest-res so highest-res wins in overlaps
        for ds in reversed(relevant):
            try:
                dst_arr = np.full((n_rows, n_cols), np.nan, dtype=np.float32)
                nodata_val = ds.nodata if ds.nodata is not None else -9999.0

                reproject(
                    source=rasterio.band(ds, 1),
                    destination=dst_arr,
                    src_transform=ds.transform,
                    src_crs=ds.crs,
                    dst_transform=target_transform,
                    dst_crs=target_crs,
                    resampling=Resampling.bilinear,
                    src_nodata=nodata_val,
                    dst_nodata=np.nan,
                )

                valid = ~np.isnan(dst_arr) & (dst_arr > -1000.0)
                result[valid] = dst_arr[valid]
            except Exception as e:
                log.warning(
                    f"load_area_array ({self.dem_dir.name}): "
                    f"errore reproiezione: {e}"
                )
                continue

        if np.all(np.isnan(result)):
            log.warning(
                f"load_area_array ({self.dem_dir.name}): "
                f"nessun dato valido nell'area richiesta"
            )
            return None, None

        meta = {
            'lat_min':      float(lat_min),
            'lat_max':      float(lat_max),
            'lon_min':      float(lon_min),
            'lon_max':      float(lon_max),
            'n_rows':       int(n_rows),
            'n_cols':       int(n_cols),
            'resolution_m': float(resolution_m),
        }

        valid_count = int(np.sum(~np.isnan(result)))
        log.info(
            f"load_area_array ({self.dem_dir.name}): "
            f"{n_rows}×{n_cols} grid "
            f"({valid_count}/{n_rows * n_cols} celle valide, "
            f"{result.nbytes / 1024 / 1024:.1f} MB)"
        )
        return result, meta

    # ------------------------------------------------------------------
    # Per-point methods (kept for fallback / utility use outside viewshed)
    # ------------------------------------------------------------------

    def get_elevation(self, lat: float, lon: float) -> Optional[float]:
        """
        Point elevation query (per-call rasterio Window reads).
        Prefer load_area_array() + shared memory in the viewshed hot path.
        """
        if not self._initialized or not self._datasets:
            return None

        for ds, bounds in zip(self._datasets, self._bounds):
            minlon, minlat, maxlon, maxlat = bounds
            if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
                try:
                    if ds.crs and ds.crs.to_epsg() != 4326:
                        transformer = Transformer.from_crs(
                            "EPSG:4326", ds.crs, always_xy=True
                        )
                        x, y = transformer.transform(lon, lat)
                    else:
                        x, y = lon, lat

                    row, col = rowcol(ds.transform, x, y)
                    if 0 <= row < ds.height and 0 <= col < ds.width:
                        val = ds.read(
                            1,
                            window=rasterio.windows.Window(col, row, 1, 1)
                        )
                        elev = float(val[0, 0])
                        if ds.nodata is not None and abs(elev - ds.nodata) < 1:
                            continue
                        if elev < -1000.0:
                            continue
                        return elev
                except Exception as e:
                    log.debug(f"get_elevation({lat},{lon}) error: {e}")
                    continue
        return None

    def get_profile(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        num_points: int = 500,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Point-by-point profile. Use _arr_profile in viewshed hot path."""
        lats = np.linspace(lat1, lat2, num_points)
        lons = np.linspace(lon1, lon2, num_points)

        distances_m = np.array([
            haversine_m(lat1, lon1, lats[i], lons[i])
            for i in range(num_points)
        ])

        elevations = np.full(num_points, np.nan)
        for i, (lat, lon) in enumerate(zip(lats, lons)):
            e = self.get_elevation(lat, lon)
            if e is not None:
                elevations[i] = e

        return distances_m, lats, elevations

    def get_grid(
        self,
        lat_min: float, lon_min: float,
        lat_max: float, lon_max: float,
        resolution_m: float = 30.0,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Elevation grid for an area."""
        dy = haversine_m(lat_min, lon_min, lat_max, lon_min)
        dx = haversine_m(lat_min, lon_min, lat_min, lon_max)
        n_lat = max(10, int(dy / resolution_m))
        n_lon = max(10, int(dx / resolution_m))

        lats = np.linspace(lat_min, lat_max, n_lat)
        lons = np.linspace(lon_min, lon_max, n_lon)
        lons_grid, lats_grid = np.meshgrid(lons, lats)

        elevations = np.full((n_lat, n_lon), np.nan)
        for i in range(n_lat):
            for j in range(n_lon):
                e = self.get_elevation(lats[i], lons[j])
                if e is not None:
                    elevations[i, j] = e

        return lats_grid, lons_grid, elevations

    def covers(self, lat: float, lon: float) -> bool:
        """True if the coordinates fall within at least one loaded file."""
        if not self._initialized or not self._datasets:
            return False
        for bounds in self._bounds:
            minlon, minlat, maxlon, maxlat = bounds
            if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
                return True
        return False

    @property
    def is_available(self) -> bool:
        """True when at least one dataset is loaded."""
        return self._initialized and len(self._datasets) > 0

    @property
    def coverage_bounds(self) -> list[tuple]:
        """List of (minlon, minlat, maxlon, maxlat) for each loaded file."""
        return list(self._bounds)

    def close(self):
        """Close all open datasets."""
        for ds in self._datasets:
            try:
                ds.close()
            except Exception:
                pass
        self._datasets = []


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6_371_000.0
K_EFFECTIVE = 4.0 / 3.0
K_EARTH_EFFECTIVE_M = EARTH_RADIUS_M * K_EFFECTIVE


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = EARTH_RADIUS_M
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r) -
         math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def earth_bulge_m(distance_m: float) -> float:
    """
    Legacy sagitta approximation d²/(2·R_eff).
    Only meaningful when distance_m is the **total** path length and
    you want the maximum bulge at the midpoint.
    Use path_earth_bulge_m() for intermediate profile points.
    """
    return (distance_m ** 2) / (2 * K_EARTH_EFFECTIVE_M)


def path_earth_bulge_m(d1_m: float, total_m: float) -> float:
    """
    Earth-bulge correction at distance d1_m from the transmitter along a
    path of total length total_m (ITU-R effective-earth-radius model).

        h = d1 · (D − d1) / (2 · k · Rₑ)

    Zero at both endpoints, maximum at the midpoint.
    """
    return (d1_m * (total_m - d1_m)) / (2 * K_EARTH_EFFECTIVE_M)


def destination_point(
    lat: float, lon: float, bearing: float, distance_m: float
) -> tuple[float, float]:
    R = EARTH_RADIUS_M
    d = distance_m / R
    b = math.radians(bearing)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(d) +
        math.cos(lat1) * math.sin(d) * math.cos(b)
    )
    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(lat1),
        math.cos(d) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# Singletons
# ---------------------------------------------------------------------------

_dem_handler: Optional[DEMHandler] = None
_dsm_handler: Optional[DEMHandler] = None
_dsm_attempted: bool = False   # avoid repeating a failed init


def get_dem_handler() -> DEMHandler:
    """Global bare-earth DTM handler."""
    global _dem_handler
    if _dem_handler is None:
        from meshcoverage.config import settings
        _dem_handler = DEMHandler(settings.dem_dir)
        _dem_handler.initialize()
    return _dem_handler


def get_dsm_handler() -> Optional[DEMHandler]:
    """
    Global DSM handler (Change F).

    Returns None when MESHCOVERAGE_DSM_DIR is not configured or the directory
    contains no GeoTIFF files.  The result is cached after the first call.
    """
    global _dsm_handler, _dsm_attempted
    if _dsm_attempted:
        return _dsm_handler

    _dsm_attempted = True
    from meshcoverage.config import settings

    if not settings.dsm_dir:
        log.info(
            "DSM non configurato (MESHCOVERAGE_DSM_DIR non impostato). "
            "Il calcolo userà solo il DTM bare-earth."
        )
        return None

    handler = DEMHandler(settings.dsm_dir)
    handler.initialize()

    if not handler.is_available:
        log.warning(
            f"DSM configurato ({settings.dsm_dir}) ma nessun file GeoTIFF trovato. "
            "Il calcolo userà solo il DTM bare-earth."
        )
        return None

    _dsm_handler = handler
    log.info(
        f"DSM disponibile: {len(handler.coverage_bounds)} file in {settings.dsm_dir}"
    )
    return _dsm_handler
