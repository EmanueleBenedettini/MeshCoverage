"""
API for information about available DEM files.

Endpoints:
  GET /api/dem               — list of loaded DEM files
  GET /api/dem/status        — status of DEMHandler
  GET /api/dem/elevation     — query elevation for coordinates
  GET /api/dem/coverage      — overall bounding box of DEMs
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from meshcoverage.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dem", tags=["dem"])


@router.get("")
async def list_dem_files():
    """Lists the DEM files present in the configured directory."""
    files = []
    for ext in ("*.tif", "*.tiff"):
        for f in settings.dem_dir.glob(ext):
            files.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                "path": str(f),
            })
    files.sort(key=lambda x: x["name"])
    return {
        "dem_dir": str(settings.dem_dir),
        "count": len(files),
        "files": files,
    }


@router.get("/status")
async def get_dem_status():
    """Status of DEMHandler: how many files are loaded, total bounding box."""
    from meshcoverage.processing.dem_handler import get_dem_handler
    dem = get_dem_handler()
    bounds = dem.coverage_bounds

    if not bounds:
        return {
            "initialized": False,
            "file_count": 0,
            "bounds": None,
            "message": "No DEM files loaded. Copy .tif files to: " + str(settings.dem_dir),
        }

    # Total bounding box
    all_minlon = min(b[0] for b in bounds)
    all_minlat = min(b[1] for b in bounds)
    all_maxlon = max(b[2] for b in bounds)
    all_maxlat = max(b[3] for b in bounds)

    return {
        "initialized": True,
        "file_count": len(bounds),
        "total_bounds": {
            "minlon": all_minlon, "minlat": all_minlat,
            "maxlon": all_maxlon, "maxlat": all_maxlat,
        },
        "individual_bounds": [
            {"minlon": b[0], "minlat": b[1], "maxlon": b[2], "maxlat": b[3]}
            for b in bounds
        ],
    }


@router.get("/elevation")
async def get_elevation(
    lat: float = Query(..., description="Latitude WGS84"),
    lon: float = Query(..., description="Longitude WGS84"),
):
    """
    Returns the elevation (m asl) for the requested coordinates.
    Useful for debugging and verifying DEM coverage.
    """
    from meshcoverage.processing.dem_handler import get_dem_handler
    dem = get_dem_handler()

    if not dem.covers(lat, lon):
        raise HTTPException(
            status_code=404,
            detail=f"Coordinates ({lat}, {lon}) outside available DEM coverage."
        )

    elev = dem.get_elevation(lat, lon)
    if elev is None:
        raise HTTPException(status_code=404, detail="No DEM data for these coordinates (nodata).")

    return {
        "lat": lat,
        "lon": lon,
        "elevation_m": round(elev, 2),
    }


@router.get("/coverage")
async def get_dem_coverage_geojson():
    """
    Returns the DEM file coverage as GeoJSON
    (one rectangle for each loaded DEM file).
    """
    from meshcoverage.processing.dem_handler import get_dem_handler
    dem = get_dem_handler()
    bounds_list = dem.coverage_bounds

    features = []
    for i, b in enumerate(bounds_list):
        minlon, minlat, maxlon, maxlat = b
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [minlon, minlat],
                    [maxlon, minlat],
                    [maxlon, maxlat],
                    [minlon, maxlat],
                    [minlon, minlat],
                ]],
            },
            "properties": {"dem_index": i},
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }
