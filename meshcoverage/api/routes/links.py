"""
API for direct connections between nodes (inter-node links).

Endpoints:
  GET  /api/links                      — list connections by freq+preset
  GET  /api/links/{freq}/{preset}      — GeoJSON connections
  GET  /api/links/node/{node_id}       — connections of a specific node
  POST /api/links/compute              — recalculate connections
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

from meshcoverage.config import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/links", tags=["links"])


def _links_path(freq: int, preset: str) -> Path:
    return settings.links_dir / f"links_{freq}_{preset}.json"


@router.get("")
async def list_link_files():
    """Lists all available connection files."""
    available = []
    for f in settings.links_dir.glob("links_*.json"):
        stem = f.stem  # links_868_MEDIUM_FAST
        parts = stem.split("_", 2)
        if len(parts) < 3:
            continue
        try:
            freq = int(parts[1])
            preset = parts[2]
        except (ValueError, IndexError):
            continue

        try:
            with open(f) as jf:
                data = json.load(jf)
            link_count = len(data.get("links", []))
            generated_at = data.get("generated_at")
        except Exception:
            link_count = 0
            generated_at = None

        available.append({
            "frequency_mhz": freq,
            "modem_preset": preset,
            "link_count": link_count,
            "generated_at": generated_at,
            "file_size_kb": round(f.stat().st_size / 1024, 1),
        })

    available.sort(key=lambda x: (x["frequency_mhz"], x["modem_preset"]))
    return available


@router.get("/{freq}/{preset}")
async def get_links(
    freq: int,
    preset: str,
    as_geojson: bool = Query(
        default=True,
        description="Returns as GeoJSON LineString (true) or raw JSON (false)"
    ),
    min_budget: float = Query(default=None, description="Filter by minimum link budget (dB)"),
    node_id: Optional[str] = Query(default=None, description="Filter by specific node"),
):
    """
    Returns direct connections between nodes by frequency and preset.
    
    In GeoJSON format: each connection is a LineString with properties
    link_budget, distance and node IDs.
    """
    path = _links_path(freq, preset)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Connections {freq}MHz / {preset} not found. Start calculation first."
        )

    with open(path) as f:
        data = json.load(f)

    links = data.get("links", [])

    # Filters
    if min_budget is not None:
        links = [l for l in links if l.get("min_link_budget", float("-inf")) >= min_budget]
    if node_id:
        nid = node_id.lower()
        links = [l for l in links if l["node_a_id"] == nid or l["node_b_id"] == nid]

    if not as_geojson:
        return {"links": links, "count": len(links)}

    # Convert to GeoJSON with node positions
    from meshcoverage import database as db
    nodes = db.load_all()

    features = []
    for link in links:
        node_a = nodes.get(link["node_a_id"])
        node_b = nodes.get(link["node_b_id"])
        if not node_a or not node_b:
            continue
        if not node_a.position or not node_b.position:
            continue

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [node_a.position.lon, node_a.position.lat],
                    [node_b.position.lon, node_b.position.lat],
                ],
            },
            "properties": {
                "node_a_id": link["node_a_id"],
                "node_b_id": link["node_b_id"],
                "node_a_name": node_a.short_name or link["node_a_id"],
                "node_b_name": node_b.short_name or link["node_b_id"],
                "distance_km": link.get("distance_km"),
                "min_link_budget": link.get("min_link_budget"),
                "link_budget_a_to_b": link.get("link_budget_a_to_b"),
                "link_budget_b_to_a": link.get("link_budget_b_to_a"),
                "los": link.get("los"),
                "fresnel_ok": link.get("fresnel_ok"),
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "frequency_mhz": freq,
            "modem_preset": preset,
            "count": len(features),
            "generated_at": data.get("generated_at"),
        },
    }


@router.get("/node/{node_id}")
async def get_node_links(
    node_id: str,
    min_budget: float = Query(default=None),
):
    """
    Returns all connections of a specific node,
    aggregated from all available links files.
    """
    from meshcoverage import database as db
    node = db.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Node {node_id!r} not found")

    all_links = []
    for f in settings.links_dir.glob("links_*.json"):
        try:
            with open(f) as jf:
                data = json.load(jf)
            for link in data.get("links", []):
                nid = node_id.lower()
                if link["node_a_id"] == nid or link["node_b_id"] == nid:
                    link["frequency_mhz"] = data.get("frequency_mhz")
                    link["modem_preset"] = data.get("modem_preset")
                    all_links.append(link)
        except Exception:
            pass

    if min_budget is not None:
        all_links = [l for l in all_links if l.get("min_link_budget", float("-inf")) >= min_budget]

    all_links.sort(key=lambda l: l.get("min_link_budget", float("-inf")), reverse=True)
    return {"node_id": node_id, "links": all_links, "count": len(all_links)}


@router.post("/compute")
async def compute_links(background_tasks: BackgroundTasks):
    """Recalculates all inter-node connections in background."""
    async def _run():
        from meshcoverage.processing.node_links import compute_node_links
        try:
            compute_node_links()
        except Exception as e:
            log.error(f"Error calculating links: {e}", exc_info=True)

    background_tasks.add_task(_run)
    return {"started": True, "message": "Connection calculation started in background"}
