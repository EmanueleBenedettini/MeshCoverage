"""
API for information about available DEM files.

Endpoints:
  GET /api/dem               — list of loaded DEM files
  GET /api/dem/status        — status of DEMHandler
  GET /api/dem/elevation     — query elevation for coordinates
  GET /api/dem/coverage      — overall bounding box of DEMs
  POST /api/dem/upload       — upload a new DEM file
  GET /api/dem/download/{filename} — download a DEM file
  GET /api/dem/thumbnail/{filename} — get thumbnail image of DEM file
  DELETE /api/dem/delete/{filename} — delete a DEM file
"""
from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional
from io import BytesIO

from fastapi import APIRouter, HTTPException, Query, UploadFile
from starlette.responses import FileResponse, StreamingResponse

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
                "size": f.stat().st_size,
            })
    files.sort(key=lambda x: x["name"])
    return {"files": files}


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


@router.post("/upload")
async def upload_dem_file(file: UploadFile):
    """Upload a new DEM file."""
    if not file.filename.lower().endswith(('.tif', '.tiff')):
        raise HTTPException(status_code=400, detail="Only .tif and .tiff files are allowed")

    file_path = settings.dem_dir / file.filename
    if file_path.exists():
        raise HTTPException(status_code=400, detail="File already exists")

    try:
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        log.info(f"Uploaded DEM file: {file.filename}")
        return {"message": "File uploaded successfully"}
    except Exception as e:
        log.error(f"Error uploading file {file.filename}: {e}")
        raise HTTPException(status_code=500, detail="Upload failed")


@router.get("/download/{filename}")
async def download_dem_file(filename: str):
    """Download a DEM file."""
    file_path = settings.dem_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=filename,
        media_type="application/octet-stream"
    )


@router.get("/thumbnail/{filename}")
async def get_dem_thumbnail(filename: str, size: int = 200):
    """Get a thumbnail image of a DEM file (cached)."""
    file_path = settings.dem_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    # Create cache directory
    cache_dir = settings.dem_dir / ".thumbnails"
    cache_dir.mkdir(exist_ok=True)

    # Cache filename includes size to support different sizes
    cache_file = cache_dir / f"{filename}_{size}.png"

    # Return cached thumbnail if it exists and is newer than the DEM file
    if cache_file.exists():
        if cache_file.stat().st_mtime > file_path.stat().st_mtime:
            return FileResponse(
                path=cache_file,
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"}
            )

    try:
        import numpy as np
        from PIL import Image
        import rasterio

        # Open the DEM file with rasterio
        with rasterio.open(str(file_path)) as src:
            data = src.read(1)

        # Downsample to thumbnail size
        from scipy import ndimage
        zoom_factor = size / max(data.shape)
        thumbnail_data = ndimage.zoom(data, zoom_factor, order=1)

        # Normalize to 0-255
        if np.isnan(thumbnail_data).all():
            raise HTTPException(status_code=400, detail="DEM file contains no valid data")

        valid_data = thumbnail_data[~np.isnan(thumbnail_data)]
        if len(valid_data) == 0:
            raise HTTPException(status_code=400, detail="DEM file contains no valid data")

        vmin, vmax = np.percentile(valid_data, [2, 98])
        if vmax == vmin:
            vmax = vmin + 1

        normalized = np.clip((thumbnail_data - vmin) / (vmax - vmin) * 255, 0, 255).astype(np.uint8)
        normalized = np.nan_to_num(normalized, nan=128)

        # Create image
        img = Image.fromarray(normalized, mode='L')
        img.thumbnail((size, size), Image.Resampling.LANCZOS)

        # Save to cache
        img.save(cache_file, format='PNG')
        log.info(f"Cached thumbnail: {cache_file}")

        # Return cached file
        return FileResponse(
            path=cache_file,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    except Exception as e:
        log.error(f"Error generating thumbnail for {filename}: {e}")
        raise HTTPException(status_code=500, detail="Cannot generate thumbnail")


@router.delete("/delete/{filename}")
async def delete_dem_file(filename: str):
    """Delete a DEM file."""
    file_path = settings.dem_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        
        # Also delete cached thumbnails for this file
        cache_dir = settings.dem_dir / ".thumbnails"
        if cache_dir.exists():
            for cache_file in cache_dir.glob(f"{filename}_*.png"):
                cache_file.unlink()
        
        log.info(f"Deleted DEM file: {filename}")
        return {"message": "File deleted successfully"}
    except Exception as e:
        log.error(f"Error deleting file {filename}: {e}")
        raise HTTPException(status_code=500, detail="Delete failed")


@router.post("/cache/clear")
async def clear_thumbnail_cache():
    """Clear all cached thumbnails."""
    cache_dir = settings.dem_dir / ".thumbnails"
    if cache_dir.exists():
        import shutil
        shutil.rmtree(cache_dir)
        log.info("Cleared thumbnail cache")
        return {"message": "Thumbnail cache cleared"}
    return {"message": "No cache to clear"}
