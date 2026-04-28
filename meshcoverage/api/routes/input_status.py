"""
API for data acquisition service status (MQTT and Direct).

Endpoints:
  GET  /api/input/status         — status of MQTT and Direct connections
  POST /api/input/start          — start input services
  POST /api/input/stop           — stop input services
  POST /api/input/test           — test connection to a direct node
"""
from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from meshcoverage.config import settings
from meshcoverage.api.dependencies import get_input_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/input", tags=["input"])


class DirectTestRequest(BaseModel):
    host: str
    port: int = 4403


@router.get("/status")
async def get_input_status(svc=Depends(get_input_service)):
    """
    Status of data acquisition services.
    Shows statistics on packets received, errors and connection status.
    """
    mqtt_stats = None
    direct_stats = None

    if svc.mqtt_client:
        mqtt_stats = {
            **svc.mqtt_client.stats,
            "broker": settings.mqtt_broker,
            "port": settings.mqtt_port,
            "topic": settings.mqtt_topic,
        }

    if svc.direct_client:
        direct_stats = {
            **svc.direct_client.stats,
            "host": settings.direct_host,
            "port": settings.direct_port,
        }

    return {
        "mqtt": {
            "enabled": settings.mqtt_enabled,
            "stats": mqtt_stats,
        },
        "direct": {
            "enabled": settings.direct_enabled,
            "stats": direct_stats,
        },
    }


@router.post("/start")
async def start_input(svc=Depends(get_input_service)):
    """Starts the configured input services."""
    if svc._running:
        return {"started": False, "message": "Services already running"}
    svc.start()
    return {"started": True, "message": "Input services started"}


@router.post("/stop")
async def stop_input(svc=Depends(get_input_service)):
    """Stops the input services."""
    svc.stop()
    return {"stopped": True, "message": "Input services stopped"}


@router.post("/test/direct")
async def test_direct_connection(req: DirectTestRequest):
    """
    Tests the reachability of a Meshtastic node via TCP.
    Useful for verifying configuration before starting the service.
    """
    import asyncio
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(req.host, req.port),
            timeout=5.0,
        )
        writer.close()
        await writer.wait_closed()
        return {
            "reachable": True,
            "host": req.host,
            "port": req.port,
            "message": f"Connection successful to {req.host}:{req.port}",
        }
    except asyncio.TimeoutError:
        return {
            "reachable": False,
            "host": req.host,
            "port": req.port,
            "message": f"Connection timeout to {req.host}:{req.port}",
        }
    except Exception as e:
        return {
            "reachable": False,
            "host": req.host,
            "port": req.port,
            "message": str(e),
        }


@router.get("/config")
async def get_input_config():
    """Returns the current input services configuration (without password)."""
    return {
        "mqtt": {
            "enabled": settings.mqtt_enabled,
            "broker": settings.mqtt_broker,
            "port": settings.mqtt_port,
            "topic": settings.mqtt_topic,
            "tls": settings.mqtt_tls,
            "has_credentials": bool(settings.mqtt_username),
        },
        "direct": {
            "enabled": settings.direct_enabled,
            "host": settings.direct_host,
            "port": settings.direct_port,
        },
    }
