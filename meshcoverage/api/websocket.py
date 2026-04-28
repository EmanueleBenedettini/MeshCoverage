"""
WebSocket manager per aggiornamenti live al frontend.
Usato per:
- Progresso calcolo copertura (per nodo e globale)
- Aggiornamenti nodi in tempo reale (nuovi pacchetti MQTT)
- Notifiche di completamento
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


class ConnectionManager:
    """Gestisce le connessioni WebSocket attive."""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        log.debug(f"WS connesso ({len(self._connections)} totali)")

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)
        log.debug(f"WS disconnesso ({len(self._connections)} totali)")

    async def broadcast(self, message: dict):
        """Invia un messaggio JSON a tutti i client connessi."""
        if not self._connections:
            return
        data = json.dumps(message, default=str)
        dead = []
        for ws in list(self._connections):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, message: dict):
        """Invia un messaggio a un singolo client."""
        try:
            await ws.send_text(json.dumps(message, default=str))
        except Exception as e:
            log.debug(f"WS send error: {e}")
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


# Singleton globale
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Tipi di messaggi
# ---------------------------------------------------------------------------

async def notify_compute_started(node_id: str | None = None):
    await manager.broadcast({
        "type": "compute_started",
        "node_id": node_id,
    })


async def notify_compute_progress(node_id: str, progress: int, total: int):
    await manager.broadcast({
        "type": "compute_progress",
        "node_id": node_id,
        "progress": progress,
        "total": total,
        "pct": int(100 * progress / total) if total > 0 else 0,
    })


async def notify_compute_done(node_id: str | None, metadata: dict | None = None):
    await manager.broadcast({
        "type": "compute_done",
        "node_id": node_id,
        "metadata": metadata,
    })


async def notify_compute_error(node_id: str | None, error: str):
    await manager.broadcast({
        "type": "compute_error",
        "node_id": node_id,
        "error": error,
    })


async def notify_node_updated(node_id: str):
    await manager.broadcast({
        "type": "node_updated",
        "node_id": node_id,
    })


# ---------------------------------------------------------------------------
# Handler endpoint WebSocket
# ---------------------------------------------------------------------------

async def ws_endpoint(websocket: WebSocket):
    """
    Endpoint WebSocket principale.
    Il client si connette e riceve aggiornamenti push.
    Può anche inviare messaggi (ping/subscribe).
    """
    await manager.connect(websocket)
    try:
        # Manda stato iniziale
        await manager.send_to(websocket, {
            "type": "connected",
            "clients": manager.count,
        })

        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_text(), timeout=30.0
                )
                msg = json.loads(data)
                # Gestisci ping
                if msg.get("type") == "ping":
                    await manager.send_to(websocket, {"type": "pong"})
            except asyncio.TimeoutError:
                # Heartbeat
                await manager.send_to(websocket, {"type": "heartbeat"})
            except json.JSONDecodeError:
                pass

    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
