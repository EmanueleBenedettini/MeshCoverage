"""
Routes for frontend HTML pages.
Serves Jinja2 pages with necessary initial data.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from meshcoverage import database
from meshcoverage.config import settings
from meshcoverage.models.node import MODEM_PRESETS

log = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

_templates_dir = Path(__file__).parent.parent.parent / "web" / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Main page with the map."""
    nodes = database.load_all()

    # Available frequencies and presets in the database
    freqs = sorted(set(n.frequency_mhz for n in nodes.values() if n.frequency_mhz))
    presets = sorted(set(n.modem_preset for n in nodes.values() if n.modem_preset))

    # Presets with description
    preset_info = {
        name: params.get("description", name)
        for name, params in MODEM_PRESETS.items()
    }

    return templates.TemplateResponse("index.html", {
        "request": request,
        "title": "MeshCoverage",
        "node_count": len(nodes),
        "available_freqs": freqs if freqs else [433, 868, 915],
        "available_presets": presets if presets else list(MODEM_PRESETS.keys()),
        "preset_info": json.dumps(preset_info),
    })


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request):
    """Node database management page."""
    nodes = list(database.load_all().values())
    nodes.sort(key=lambda n: n.last_seen or "", reverse=True)

    return templates.TemplateResponse("nodes.html", {
        "request": request,
        "title": "MeshCoverage — Node Management",
        "nodes": nodes,
        "total": len(nodes),
        "complete": sum(1 for n in nodes if n.is_complete),
        "presets": list(MODEM_PRESETS.keys()),
        "preset_descriptions": {k: v["description"] for k, v in MODEM_PRESETS.items()},
    })


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings and service status page."""
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "title": "MeshCoverage — Settings",
        "config": {
            "mqtt_enabled": settings.mqtt_enabled,
            "mqtt_broker": settings.mqtt_broker,
            "mqtt_port": settings.mqtt_port,
            "mqtt_topic": settings.mqtt_topic,
            "direct_enabled": settings.direct_enabled,
            "direct_host": settings.direct_host,
            "direct_port": settings.direct_port,
            "compute_schedule": settings.compute_schedule,
            "dem_dir": str(settings.dem_dir),
            "max_range_km": settings.max_range_km,
            "dem_resolution": settings.dem_resolution,
        },
    })
