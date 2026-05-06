"""
API for aggregated coverage heatmaps.

Endpoints:
  GET /api/heatmaps                               — list available heatmaps
  GET /api/heatmaps/{freq}/{preset}               — GeoJSON heatmap for freq+preset
  GET /api/heatmaps/{freq}/{preset}/metadata      — heatmap metadata
  GET /api/heatmaps/{freq}/{preset}/image         — georeferenced PNG for coverage
  GET /api/heatmaps/{freq}/{preset}/shadows       — GeoJSON shadow zones
  GET /api/heatmaps/{freq}/{preset}/shadows/image — georeferenced PNG for shadows
  POST /api/heatmaps/generate                     — regenerate all heatmaps
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

from meshcoverage.config import settings
from meshcoverage.models.node import MODEM_PRESETS, VALID_FREQUENCIES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/heatmaps", tags=["heatmaps"])


def _heatmap_path(freq: int, preset: str) -> Path:
    return settings.heatmaps_dir / f"heatmap_{freq}_{preset}.geojson"


def _heatmap_meta_path(freq: int, preset: str) -> Path:
    return settings.heatmaps_dir / f"heatmap_{freq}_{preset}_meta.json"


def _shadow_path(freq: int, preset: str) -> Path:
    return settings.heatmaps_dir / f"shadows_{freq}_{preset}.geojson"


@router.get("")
async def list_heatmaps():
    """Lists all available heatmaps with frequency, preset and basic metadata."""
    available = []
    for f in settings.heatmaps_dir.glob("heatmap_*_*.geojson"):
        stem = f.stem  # heatmap_868_MEDIUM_FAST
        parts = stem.split("_", 2)
        if len(parts) < 3:
            continue
        try:
            freq = int(parts[1])
            preset = parts[2]
        except (ValueError, IndexError):
            continue

        meta = {}
        meta_path = _heatmap_meta_path(freq, preset)
        if meta_path.exists():
            with open(meta_path) as mf:
                meta = json.load(mf)

        available.append({
            "frequency_mhz": freq,
            "modem_preset": preset,
            "file_size_kb": round(f.stat().st_size / 1024, 1),
            "generated_at": meta.get("generated_at"),
            "node_count": meta.get("node_count"),
            "point_count": meta.get("point_count"),
            "shadow_point_count": meta.get("shadow_point_count"),
            "has_shadows": _shadow_path(freq, preset).exists(),
        })

    available.sort(key=lambda x: (x["frequency_mhz"], x["modem_preset"]))
    return available


@router.get("/{freq}/{preset}/metadata")
async def get_heatmap_metadata(freq: int, preset: str):
    """Metadata for heatmap by frequency and preset."""
    meta_path = _heatmap_meta_path(freq, preset)
    if not meta_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Heatmap {freq}MHz / {preset} not found. Run calculation first."
        )
    with open(meta_path) as f:
        return json.load(f)


@router.get("/{freq}/{preset}/shadows/image")
async def get_shadow_zones_image(freq: int, preset: str):
    """
    Returns aggregated terrain shadow zones as a georeferenced PNG
    suitable for L.imageOverlay.

    Shadow points are rendered with a dark-indigo palette, visually
    distinct from the coverage heatmap so both layers can be shown
    simultaneously without confusion. Shadows are masked out where
    coverage exists to prevent overlap.

    Response: { image: "data:image/png;base64,...",
                bounds: [[lat_min, lon_min], [lat_max, lon_max]] }
    """
    import numpy as np
    from meshcoverage.processing.raster_renderer import render_shadow_png, GRID_DEG
    from scipy.ndimage import gaussian_filter

    path = _shadow_path(freq, preset)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Shadow data for {freq}MHz / {preset} not found. "
                "Run calculation first (shadows are generated alongside heatmaps)."
            )
        )

    with open(path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    if not features:
        raise HTTPException(status_code=404, detail="No shadow data available.")

    shadow_lats = np.array([ft["geometry"]["coordinates"][1] for ft in features], dtype=np.float32)
    shadow_lons = np.array([ft["geometry"]["coordinates"][0] for ft in features], dtype=np.float32)

    # Get coverage points for masking
    heatmap_path = _heatmap_path(freq, preset)
    with open(heatmap_path) as f:
        coverage_geojson = json.load(f)

    coverage_features = coverage_geojson.get("features", [])
    coverage_lats = np.array([ft["geometry"]["coordinates"][1] for ft in coverage_features], dtype=np.float32) if coverage_features else np.array([], dtype=np.float32)
    coverage_lons = np.array([ft["geometry"]["coordinates"][0] for ft in coverage_features], dtype=np.float32) if coverage_features else np.array([], dtype=np.float32)

    # Combined bounds
    all_lats = np.concatenate([shadow_lats, coverage_lats])
    all_lons = np.concatenate([shadow_lons, coverage_lons])
    if len(all_lats) == 0:
        raise HTTPException(status_code=404, detail="No data.")

    grid_deg = GRID_DEG
    lat_min = round(float(all_lats.min()) / grid_deg) * grid_deg - grid_deg
    lat_max = round(float(all_lats.max()) / grid_deg) * grid_deg + grid_deg
    lon_min = round(float(all_lons.min()) / grid_deg) * grid_deg - grid_deg
    lon_max = round(float(all_lons.max()) / grid_deg) * grid_deg + grid_deg

    n_rows_raw = int(round((lat_max - lat_min) / grid_deg)) + 1
    n_cols_raw = int(round((lon_max - lon_min) / grid_deg)) + 1

    from meshcoverage.processing.raster_renderer import MAX_DIM
    scale = min(1.0, MAX_DIM / max(n_rows_raw, n_cols_raw, 1))
    n_rows = max(4, int(n_rows_raw * scale))
    n_cols = max(4, int(n_cols_raw * scale))

    # Compute coverage weight
    coverage_weight = None
    if len(coverage_lats) > 0:
        grid = np.full((n_rows, n_cols), np.nan, dtype=np.float32)
        r_idx = np.clip(
            np.round((lat_max - coverage_lats) / (lat_max - lat_min) * (n_rows - 1)).astype(int),
            0, n_rows - 1,
        )
        c_idx = np.clip(
            np.round((coverage_lons - lon_min) / (lon_max - lon_min) * (n_cols - 1)).astype(int),
            0, n_cols - 1,
        )
        for r, c in zip(r_idx, c_idx):
            if np.isnan(grid[r, c]):
                grid[r, c] = 1.0
        valid = (~np.isnan(grid)).astype(np.float32)
        sigma = 1.8 / scale if scale < 1.0 else 1.8
        coverage_weight = gaussian_filter(valid, sigma=sigma)

    result = render_shadow_png(
        shadow_lats, shadow_lons,
        bounds_from_lats=all_lats,
        bounds_from_lons=all_lons,
        mask_alpha_where_weight_nonzero=coverage_weight,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No renderable shadow data.")

    return result


@router.get("/{freq}/{preset}/shadows")
async def get_shadow_zones(
    freq: int,
    preset: str,
    bbox: Optional[str] = Query(
        default=None,
        description="Bounding box: 'minlon,minlat,maxlon,maxlat'"
    ),
    max_distance_km: float = Query(
        default=None,
        description="Only include shadow zones within this distance from any node (km)"
    ),
):
    """
    Returns aggregated terrain shadow zones as GeoJSON.

    Shadow zones are areas within the analysis range where terrain blocks
    line of sight from every node on this frequency+preset combination.
    These are displayed on the map with a distinct hatched/dark overlay
    to make dead zones visually obvious.

    Points that are covered by at least one node are excluded — only
    genuinely unreachable terrain shadows appear here.
    """
    path = _shadow_path(freq, preset)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Shadow data for {freq}MHz / {preset} not found. "
                "Run calculation first (shadows are generated alongside heatmaps)."
            )
        )

    # Serve file directly if no filters requested
    if bbox is None and max_distance_km is None:
        return FileResponse(
            path,
            media_type="application/geo+json",
            filename=f"shadows_{freq}_{preset}.geojson",
        )

    with open(path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    if max_distance_km is not None:
        max_dist_m = max_distance_km * 1000.0
        features = [
            feat for feat in features
            if feat["properties"].get("distance_m", 0) <= max_dist_m
        ]

    if bbox:
        try:
            minlon, minlat, maxlon, maxlat = map(float, bbox.split(","))
            features = [
                feat for feat in features
                if (minlon <= feat["geometry"]["coordinates"][0] <= maxlon and
                    minlat <= feat["geometry"]["coordinates"][1] <= maxlat)
            ]
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid bbox. Format: minlon,minlat,maxlon,maxlat"
            )

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            **geojson.get("properties", {}),
            "filtered_count": len(features),
        },
    }


@router.get("/{freq}/{preset}/image")
async def get_heatmap_image(
    freq: int,
    preset: str,
    min_budget: float = Query(default=None, description="Minimum link budget (dB)"),
):
    """
    Returns the heatmap as a georeferenced PNG for L.imageOverlay.
    Response: { image: "data:image/png;base64,...", bounds: [[lat_min,lon_min],[lat_max,lon_max]] }
    """
    import numpy as np
    from meshcoverage.processing.raster_renderer import render_coverage_png

    path = _heatmap_path(freq, preset)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Heatmap {freq}MHz / {preset} not found. Run calculation first."
        )

    with open(path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    threshold = min_budget if min_budget is not None else settings.min_link_budget_db
    features = [
        ft for ft in features
        if ft["properties"].get("link_budget_db", float("-inf")) >= threshold
    ]

    if not features:
        raise HTTPException(status_code=404, detail="No data above threshold.")

    lons = np.array([ft["geometry"]["coordinates"][0] for ft in features], dtype=np.float32)
    lats = np.array([ft["geometry"]["coordinates"][1] for ft in features], dtype=np.float32)
    lbs  = np.array([ft["properties"]["link_budget_db"]  for ft in features], dtype=np.float32)

    result = render_coverage_png(lats, lons, lbs)
    if result is None:
        raise HTTPException(status_code=404, detail="No renderable data.")

    return result


@router.get("/{freq}/{preset}")
async def get_heatmap(
    freq: int,
    preset: str,
    min_budget: float = Query(default=None, description="Filter by minimum link budget (dB)"),
    bbox: Optional[str] = Query(
        default=None,
        description="Bounding box: 'minlon,minlat,maxlon,maxlat' to filter area"
    ),
):
    """
    Returns the aggregated heatmap as GeoJSON FeatureCollection.
    For each point, the maximum link budget among all nodes is present.

    Optional parameters:
    - min_budget: filters points below threshold
    - bbox: crops area of interest
    """
    path = _heatmap_path(freq, preset)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Heatmap {freq}MHz / {preset} not found. Start calculation first."
        )

    # Serve file directly when no filters needed
    if min_budget is None and bbox is None:
        return FileResponse(
            path,
            media_type="application/geo+json",
            filename=f"heatmap_{freq}_{preset}.geojson",
        )

    # Otherwise filter in memory
    with open(path) as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    if min_budget is not None:
        features = [
            feat for feat in features
            if feat["properties"].get("link_budget_db", float("-inf")) >= min_budget
        ]

    if bbox:
        try:
            minlon, minlat, maxlon, maxlat = map(float, bbox.split(","))
            filtered = []
            for feat in features:
                coords = feat["geometry"]["coordinates"]
                lon, lat = coords[0], coords[1]
                if minlon <= lon <= maxlon and minlat <= lat <= maxlat:
                    filtered.append(feat)
            features = filtered
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Invalid bbox. Format: minlon,minlat,maxlon,maxlat"
            )

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            **geojson.get("properties", {}),
            "filtered_count": len(features),
        },
    }


@router.post("/generate")
async def generate_heatmaps(background_tasks: BackgroundTasks):
    """
    Regenerates all aggregated heatmaps (and shadow zones) from saved coverage data.
    Executed in background.
    """
    async def _run():
        from meshcoverage.processing.heatmap_generator import generate_heatmaps as _gen
        try:
            _gen()
            log.info("Heatmaps and shadow zones regenerated successfully")
        except Exception as e:
            log.error(f"Error generating heatmaps: {e}", exc_info=True)

    background_tasks.add_task(_run)
    return {"started": True, "message": "Heatmap + shadow zone generation started in background"}
