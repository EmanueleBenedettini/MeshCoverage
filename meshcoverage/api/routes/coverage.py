"""
API per il calcolo e la lettura della copertura radio.

Endpoints:
  POST /api/coverage/compute/all          — avvia calcolo per tutti i nodi
  POST /api/coverage/compute/{node_id}    — avvia calcolo per nodo singolo
  GET  /api/coverage/status               — stato calcolo globale
  GET  /api/coverage/status/{node_id}     — stato calcolo nodo specifico
  GET  /api/coverage/{node_id}/geojson    — copertura nodo come GeoJSON
  GET  /api/coverage/{node_id}/shadows    — shadow zones come GeoJSON   ← NEW
  GET  /api/coverage/{node_id}/metadata   — metadati calcolo nodo
  GET  /api/coverage/available            — lista nodi con copertura calcolata
"""
from __future__ import annotations
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from meshcoverage import database
from meshcoverage.config import settings
from meshcoverage.api.dependencies import get_calculator
from meshcoverage.api.websocket import (
    notify_compute_started, notify_compute_progress,
    notify_compute_done, notify_compute_error,
)
from meshcoverage.processing.coverage_calculator import CoverageCalculator

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/coverage", tags=["coverage"])

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="coverage-worker")
_compute_running = False


# ---------------------------------------------------------------------------
# Schemi
# ---------------------------------------------------------------------------

class ComputeResponse(BaseModel):
    started: bool
    message: str
    node_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _viewshed_to_geojson(node_id: str) -> Optional[dict]:
    """Converte il file NPZ di copertura in GeoJSON FeatureCollection."""
    from meshcoverage.processing.viewshed import load_viewshed

    safe_id = node_id.lstrip("!").lower()
    path = settings.coverage_dir / f"coverage_{safe_id}.npz"
    data = load_viewshed(path)

    if data is None or len(data["lats"]) == 0:
        return None

    mask = data["link_budget"] >= settings.min_link_budget_db

    features = []
    for i in range(len(data["lats"])):
        if not mask[i]:
            continue
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    round(float(data["lons"][i]), 6),
                    round(float(data["lats"][i]), 6),
                ],
            },
            "properties": {
                "link_budget_db": round(float(data["link_budget"][i]), 2),
                "distance_m": round(float(data["distances"][i])),
                "los": bool(data["los"][i]),
                "fresnel_ok": bool(data["fresnel_ok"][i]),
                "node_id": node_id,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {"node_id": node_id, "count": len(features)},
    }


def _make_ws_progress_cb(loop: asyncio.AbstractEventLoop):
    """
    Returns a thread-safe progress callback that forwards updates to
    connected WebSocket clients via notify_compute_progress.
    Called from inside the ThreadPoolExecutor worker thread.
    """
    def _cb(node_id: str, done: int, total: int):
        asyncio.run_coroutine_threadsafe(
            notify_compute_progress(node_id, done, total),
            loop,
        )
    return _cb


async def _run_compute_all(calc: CoverageCalculator, force: bool):
    global _compute_running
    _compute_running = True
    await notify_compute_started(None)
    try:
        loop = asyncio.get_event_loop()
        progress_cb = _make_ws_progress_cb(loop)
        result = await loop.run_in_executor(
            _executor,
            lambda: calc.compute_all(force=force, node_progress_callback=progress_cb),
        )
        await notify_compute_done(None, {
            "total": result.get("total"),
            "computed": result.get("computed"),
        })
    except Exception as e:
        log.error(f"Errore compute_all: {e}", exc_info=True)
        await notify_compute_error(None, str(e))
    finally:
        _compute_running = False


async def _run_compute_node(calc: CoverageCalculator, node_id: str, force: bool):
    await notify_compute_started(node_id)
    try:
        node = database.get_node(node_id)
        if not node:
            await notify_compute_error(node_id, "Nodo non trovato")
            return
        loop = asyncio.get_event_loop()
        progress_cb = _make_ws_progress_cb(loop)
        result = await loop.run_in_executor(
            _executor,
            lambda: calc.compute_node(node, force=force, progress_callback=progress_cb),
        )
        await notify_compute_done(node_id, result.get("metadata"))
    except Exception as e:
        log.error(f"Errore compute_node {node_id}: {e}", exc_info=True)
        await notify_compute_error(node_id, str(e))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/compute/all", response_model=ComputeResponse)
async def compute_all(
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    calc: CoverageCalculator = Depends(get_calculator),
):
    """Avvia il calcolo di copertura per tutti i nodi completi (background)."""
    global _compute_running
    if _compute_running:
        return ComputeResponse(started=False, message="Calcolo già in corso")
    background_tasks.add_task(_run_compute_all, calc, force)
    return ComputeResponse(started=True, message="Calcolo avviato in background")


@router.post("/compute/{node_id}", response_model=ComputeResponse)
async def compute_node(
    node_id: str,
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False),
    calc: CoverageCalculator = Depends(get_calculator),
):
    """Avvia il calcolo di copertura per un nodo specifico."""
    node = database.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")
    if not node.is_complete:
        raise HTTPException(status_code=422, detail="Dati nodo incompleti per il calcolo")
    background_tasks.add_task(_run_compute_node, calc, node_id, force)
    return ComputeResponse(
        started=True, message=f"Calcolo avviato per {node_id}", node_id=node_id
    )


@router.get("/status")
async def get_global_status(calc: CoverageCalculator = Depends(get_calculator)):
    return {"running": _compute_running, "nodes": calc.get_status()}


@router.get("/status/{node_id}")
async def get_node_status(
    node_id: str,
    calc: CoverageCalculator = Depends(get_calculator),
):
    return calc.get_status(node_id)


@router.get("/available")
async def list_available():
    results = []
    for meta_file in settings.coverage_dir.glob("metadata_*.json"):
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            results.append(meta)
        except Exception:
            pass
    results.sort(key=lambda m: m.get("computed_at", ""), reverse=True)
    return results


@router.get("/{node_id}/metadata")
async def get_node_metadata(node_id: str):
    safe_id = node_id.lstrip("!").lower()
    meta_path = settings.coverage_dir / f"metadata_{safe_id}.json"
    if not meta_path.exists():
        raise HTTPException(
            status_code=404, detail="Metadati non trovati. Eseguire prima il calcolo."
        )
    with open(meta_path) as f:
        return json.load(f)
    
    
@router.get("/{node_id}/image")
async def get_node_coverage_image(
    node_id: str,
    min_budget: float = Query(default=None, description="Minimum link margin (dB)"),
):
    """
    Returns the single-node coverage as a georeferenced PNG for L.imageOverlay.
    Response: { image: "data:image/png;base64,...", bounds: [[lat_min,lon_min],[lat_max,lon_max]] }
    """
    from meshcoverage.processing.viewshed import load_viewshed
    from meshcoverage.processing.raster_renderer import render_coverage_png

    safe_id = node_id.lstrip("!").lower()
    path = settings.coverage_dir / f"coverage_{safe_id}.npz"

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Coverage not yet computed for this node. Run calculation first.",
        )

    data = load_viewshed(path)
    if data is None or len(data["lats"]) == 0:
        raise HTTPException(status_code=404, detail="No coverage data found.")

    threshold = min_budget if min_budget is not None else settings.min_link_margin_db
    mask = data["link_margin_db"] >= threshold

    lats = data["lats"][mask].astype(np.float32)
    lons = data["lons"][mask].astype(np.float32)
    lbs  = data["link_margin_db"][mask].astype(np.float32)

    if len(lats) == 0:
        raise HTTPException(status_code=404, detail="No points above the requested threshold.")

    result = render_coverage_png(lats, lons, lbs)
    if result is None:
        raise HTTPException(status_code=404, detail="Renderer returned no data.")

    return result


@router.get("/{node_id}/geojson")
async def get_node_coverage_geojson(
    node_id: str,
    min_budget: float = Query(default=None),
    los_only: bool = Query(default=False),
    fresnel_only: bool = Query(default=False),
):
    """Restituisce la copertura del nodo come GeoJSON FeatureCollection."""
    from meshcoverage.processing.viewshed import load_viewshed

    safe_id = node_id.lstrip("!").lower()
    path = settings.coverage_dir / f"coverage_{safe_id}.npz"

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Copertura non calcolata. Avviare prima il calcolo.",
        )

    data = load_viewshed(path)
    if data is None or len(data["lats"]) == 0:
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {"node_id": node_id},
        }

    threshold = min_budget if min_budget is not None else settings.min_link_budget_db
    mask = data["link_budget"] >= threshold

    if los_only:
        mask &= data["los"]
    if fresnel_only:
        mask &= data["fresnel_ok"]

    indices = np.where(mask)[0]

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    round(float(data["lons"][i]), 6),
                    round(float(data["lats"][i]), 6),
                ],
            },
            "properties": {
                "link_budget_db": round(float(data["link_budget"][i]), 2),
                "distance_m": round(float(data["distances"][i])),
                "los": bool(data["los"][i]),
                "fresnel_ok": bool(data["fresnel_ok"][i]),
                "node_id": node_id,
            },
        }
        for i in indices
    ]

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "node_id": node_id,
            "count": len(features),
            "filtered": len(indices) < len(data["lats"]),
        },
    }



@router.get("/{node_id}/shadows")
async def get_node_shadow_zones(
    node_id: str,
    max_distance_km: float = Query(default=None, description="Limit shadow zones to this distance (km)"),
):
    """
    Returns terrain shadow zones for the node as a GeoJSON FeatureCollection.

    Shadow zones are areas where the terrain blocks line of sight from the antenna.
    These points are within the antenna's beam sector and DEM coverage but have
    no direct radio path due to terrain obstruction.

    Useful for overlaying on the map alongside the coverage heatmap to show
    where signal cannot reach due to terrain (as opposed to distance).
    """
    from meshcoverage.processing.viewshed import load_viewshed

    safe_id = node_id.lstrip("!").lower()
    path = settings.coverage_dir / f"coverage_{safe_id}.npz"

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Copertura non calcolata. Avviare prima il calcolo.",
        )

    data = load_viewshed(path)
    if data is None:
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {"node_id": node_id, "shadow_count": 0},
        }

    shadow_lats = data.get("shadow_lats", np.array([]))
    shadow_lons = data.get("shadow_lons", np.array([]))
    shadow_distances = data.get("shadow_distances", np.array([]))

    if len(shadow_lats) == 0:
        return {
            "type": "FeatureCollection",
            "features": [],
            "properties": {
                "node_id": node_id,
                "shadow_count": 0,
                "note": "No shadow data. Recompute coverage to include shadow zones.",
            },
        }

    # Optional distance filter
    if max_distance_km is not None:
        max_dist_m = max_distance_km * 1000.0
        mask = shadow_distances <= max_dist_m
        shadow_lats = shadow_lats[mask]
        shadow_lons = shadow_lons[mask]
        shadow_distances = shadow_distances[mask]

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    round(float(shadow_lons[i]), 6),
                    round(float(shadow_lats[i]), 6),
                ],
            },
            "properties": {
                "distance_m": round(float(shadow_distances[i])),
                "node_id": node_id,
                "shadow": True,
            },
        }
        for i in range(len(shadow_lats))
    ]

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "node_id": node_id,
            "shadow_count": len(features),
        },
    }
