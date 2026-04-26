"""
Gestione file DEM (Digital Elevation Model) in formato GeoTIFF.
Supporta file multipli affiancati, indicizzati automaticamente.
"""
from __future__ import annotations
import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Lazy import rasterio (potrebbe non essere disponibile in tutti gli ambienti)
try:
    import rasterio
    from rasterio.transform import rowcol, xy
    from rasterio.merge import merge as rasterio_merge
    from rasterio.windows import from_bounds
    from pyproj import Transformer
    HAS_RASTERIO = True
except ImportError:
    HAS_RASTERIO = False
    log.warning("rasterio non disponibile. Funzionalità DEM disabilitate.")


class DEMHandler:
    """
    Gestore file DEM.
    Carica e indicizza tutti i file GeoTIFF in una directory.
    Fornisce query di elevazione per coordinate lat/lon.
    """

    def __init__(self, dem_dir: Path):
        self.dem_dir = dem_dir
        self._datasets: list = []       # rasterio datasets aperti
        self._bounds: list[tuple] = []  # (minlon, minlat, maxlon, maxlat)
        self._merged_data: Optional[np.ndarray] = None
        self._merged_transform = None
        self._merged_crs = None
        self._cached_bounds: Optional[tuple] = None  # bbox del merged dataset
        self._initialized = False

    def initialize(self):
        """Scansiona la directory DEM e indicizza tutti i file GeoTIFF."""
        if not HAS_RASTERIO:
            log.error("rasterio non disponibile, impossibile usare DEM")
            return

        self._datasets = []
        self._bounds = []

        tif_files = sorted(self.dem_dir.glob("*.tif")) + sorted(self.dem_dir.glob("*.tiff"))
        if not tif_files:
            log.warning(f"Nessun file DEM trovato in {self.dem_dir}")
            return

        log.info(f"Trovati {len(tif_files)} file DEM in {self.dem_dir}")

        for f in tif_files:
            try:
                ds = rasterio.open(f)
                # Converti bounds in lat/lon (WGS84)
                if ds.crs and ds.crs.to_epsg() != 4326:
                    transformer = Transformer.from_crs(
                        ds.crs, "EPSG:4326", always_xy=True
                    )
                    left, top = transformer.transform(ds.bounds.left, ds.bounds.top)
                    right, bottom = transformer.transform(ds.bounds.right, ds.bounds.bottom)
                    bounds_ll = (
                        min(left, right), min(top, bottom),
                        max(left, right), max(top, bottom)
                    )
                else:
                    b = ds.bounds
                    bounds_ll = (b.left, b.bottom, b.right, b.top)

                self._datasets.append(ds)
                self._bounds.append(bounds_ll)
                log.debug(f"DEM caricato: {f.name} bounds={bounds_ll}")
            except Exception as e:
                log.error(f"Errore apertura DEM {f}: {e}")

        self._initialized = True
        log.info(f"Inizializzati {len(self._datasets)} file DEM")

    def _get_dataset_for(self, lat: float, lon: float) -> Optional[tuple]:
        """Trova il dataset DEM che copre le coordinate date."""
        for ds, bounds in zip(self._datasets, self._bounds):
            minlon, minlat, maxlon, maxlat = bounds
            if minlat <= lat <= maxlat and minlon <= lon <= maxlon:
                return ds, bounds
        return None, None

    def get_elevation(self, lat: float, lon: float) -> Optional[float]:
        """
        Restituisce l'elevazione (metri slm) per le coordinate date.
        Returns None se fuori dalla copertura DEM.
        """
        if not self._initialized or not self._datasets:
            return None

        result = self._get_dataset_for(lat, lon)
        if result[0] is None:
            return None

        ds, _ = result
        try:
            # Trasforma lat/lon nel CRS del dataset
            if ds.crs and ds.crs.to_epsg() != 4326:
                transformer = Transformer.from_crs(
                    "EPSG:4326", ds.crs, always_xy=True
                )
                x, y = transformer.transform(lon, lat)
            else:
                x, y = lon, lat

            row, col = rowcol(ds.transform, x, y)
            if 0 <= row < ds.height and 0 <= col < ds.width:
                val = ds.read(1, window=rasterio.windows.Window(col, row, 1, 1))
                elev = float(val[0, 0])
                # Nodata check
                if ds.nodata is not None and abs(elev - ds.nodata) < 1:
                    return None
                return elev
        except Exception as e:
            log.debug(f"get_elevation({lat},{lon}) error: {e}")
        return None

    def get_profile(
        self,
        lat1: float, lon1: float,
        lat2: float, lon2: float,
        num_points: int = 500,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Estrae il profilo di elevazione tra due punti.

        Returns:
            distances_m: array distanze in metri dal punto 1
            lats: array latitudini dei punti del profilo
            elevations: array elevazioni (m slm), NaN dove dati mancanti
        """
        lats = np.linspace(lat1, lat2, num_points)
        lons = np.linspace(lon1, lon2, num_points)

        # Calcola distanze progressive in metri
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
        """
        Restituisce una griglia di elevazioni per un'area.

        Returns:
            lats_grid: array 2D latitudini
            lons_grid: array 2D longitudini
            elevations: array 2D elevazioni
        """
        # Calcola numero di punti
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
        """True se le coordinate sono coperte da almeno un file DEM."""
        return self._get_dataset_for(lat, lon)[0] is not None

    @property
    def coverage_bounds(self) -> list[tuple]:
        """Lista di bounding box (minlon, minlat, maxlon, maxlat) dei file DEM."""
        return list(self._bounds)

    def close(self):
        """Chiude tutti i dataset aperti."""
        for ds in self._datasets:
            try:
                ds.close()
            except Exception:
                pass
        self._datasets = []


# ---------------------------------------------------------------------------
# Funzioni geometriche
# ---------------------------------------------------------------------------

EARTH_RADIUS_M = 6_371_000.0
K_EFFECTIVE = 4.0 / 3.0  # Fattore raggio efficace Terra (atmosfera standard)
K_EARTH_EFFECTIVE_M = EARTH_RADIUS_M * K_EFFECTIVE


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza in metri tra due punti lat/lon (formula haversine)."""
    R = EARTH_RADIUS_M
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcola il bearing (azimuth) da punto 1 a punto 2 in gradi [0, 360)."""
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r) -
         math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    bearing = math.degrees(math.atan2(x, y))
    return (bearing + 360) % 360


def earth_bulge_m(distance_m: float) -> float:
    """
    Rigonfiamento terrestre a una data distanza.
    Con fattore di rifrazione atmosferica standard (k=4/3).
    Formula: h = d² / (2 * R_eff)
    """
    return (distance_m ** 2) / (2 * K_EARTH_EFFECTIVE_M)


def destination_point(
    lat: float, lon: float, bearing: float, distance_m: float
) -> tuple[float, float]:
    """
    Calcola le coordinate di un punto a una certa distanza e direzione.
    """
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


# Singleton globale
_dem_handler: Optional[DEMHandler] = None


def get_dem_handler() -> DEMHandler:
    """Restituisce l'istanza globale del DEMHandler (inizializzato se necessario)."""
    global _dem_handler
    if _dem_handler is None:
        from meshmonitor.config import settings
        _dem_handler = DEMHandler(settings.dem_dir)
        _dem_handler.initialize()
    return _dem_handler
