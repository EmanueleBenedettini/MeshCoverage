"""
API for aggregated coverage heatmaps.

Endpoints:
  GET /api/heatmaps                          — list available heatmaps
  GET /api/heatmaps/{freq}/{preset}          — GeoJSON heatmap for freq+preset
  GET /api/heatmaps/{freq}/{preset}/metadata — heatmap metadata
  POST /api/heatmaps/generate                — regenerate all heatmaps
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


@router.get("")
async def list_heatmaps():
    """
    Lists all available heatmaps with frequency, preset and basic metadata.
    """
    available = []
    for f in settings.heatmaps_dir.glob("heatmap_*_*.geojson"):
        # Parse filename: heatmap_868_MEDIUM_FAST.geojson
        stem = f.stem  # heatmap_868_MEDIUM_FAST
        parts = stem.split("_", 2)  # ["heatmap", "868", "MEDIUM_FAST"]
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

    # If no filters, serve the file directly
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
            raise HTTPException(status_code=400, detail="Invalid bbox. Format: minlon,minlat,maxlon,maxlat")

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
    Regenerates all aggregated heatmaps from saved coverage data.
    Executed in background.
    """
    async def _run():
        from meshcoverage.processing.heatmap_generator import generate_heatmaps as _gen
        try:
            _gen()
            log.info("Heatmaps regenerated successfully")
        except Exception as e:
            log.error(f"Error generating heatmaps: {e}", exc_info=True)

    background_tasks.add_task(_run)
    return {"started": True, "message": "Heatmap generation started in background"}
