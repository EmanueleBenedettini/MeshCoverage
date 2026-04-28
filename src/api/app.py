"""
FastAPI entry point for MeshCoverage.
Registers all routers, configures CORS, static files, WebSocket,
and manages service startup/shutdown.
"""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from meshcoverage.config import settings
from meshcoverage.api.routes import nodes, coverage, heatmaps, links, dem, input_status, web
from meshcoverage.api.websocket import ws_endpoint
from meshcoverage.api.dependencies import get_input_service, set_input_service

log = logging.getLogger(__name__)

_static_dir = Path(__file__).parent.parent / "web" / "static"


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Controlled startup and shutdown of the application."""
    log.info("=== MeshCoverage startup ===")
    settings.ensure_dirs()

    # Initialises DEMHandler in background (can be slow)
    import asyncio
    from meshcoverage.processing.dem_handler import get_dem_handler

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_dem_handler)

    # Start input services if configured
    if settings.mqtt_enabled or settings.direct_enabled:
        from meshcoverage.input.service import InputService
        svc = InputService()
        set_input_service(svc)
        svc.start()
        log.info("Input services started")

    log.info(f"MeshCoverage listening on http://{settings.host}:{settings.port}")
    yield

    # Shutdown
    log.info("=== MeshCoverage shutdown ===")
    try:
        svc = get_input_service()
        if svc._running:
            svc.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MeshCoverage API",
    description=(
        "Monitoring and coverage calculation system for Meshtastic mesh networks. "
        "Collects data from nodes via MQTT or direct connection, calculates radio coverage "
        "using DEM data and presents results on an interactive map."
    ),
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_route(websocket: WebSocket):
    """
    WebSocket for live updates.
    Messages:
    - compute_started / compute_progress / compute_done / compute_error
    - node_updated
    - heartbeat / pong
    """
    await ws_endpoint(websocket)

# ---------------------------------------------------------------------------
# Static files (CSS, JS, images)
# ---------------------------------------------------------------------------

if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ---------------------------------------------------------------------------
# API Router
# ---------------------------------------------------------------------------

app.include_router(nodes.router)
app.include_router(coverage.router)
app.include_router(heatmaps.router)
app.include_router(links.router)
app.include_router(dem.router)
app.include_router(input_status.router)

# Web pages router (last, to avoid conflicts with /api/*)
app.include_router(web.router)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/api/health", tags=["system"])
async def health():
    """Health check endpoint."""
    from meshcoverage import database as db
    nodes = db.load_all()
    return {
        "status": "ok",
        "version": "1.0.0",
        "node_count": len(nodes),
        "complete_nodes": sum(1 for n in nodes.values() if n.is_complete),
    }


@app.get("/api/version", tags=["system"])
async def version():
    return {"version": "1.0.0", "name": "MeshCoverage"}
