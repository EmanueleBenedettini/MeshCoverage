"""
API CRUD per il database nodi.

Endpoints:
  GET    /api/nodes              — lista tutti i nodi
  GET    /api/nodes/{node_id}    — dettaglio nodo
  POST   /api/nodes              — crea nuovo nodo manuale
  PUT    /api/nodes/{node_id}    — aggiorna nodo (completo)
  PATCH  /api/nodes/{node_id}    — aggiorna parzialmente (es. solo antenna)
  DELETE /api/nodes/{node_id}    — elimina nodo
  GET    /api/nodes/presets       — lista preset modem con parametri
  GET    /api/nodes/frequencies   — frequenze supportate
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from meshmonitor import database
from meshmonitor.models.node import Node, NodeUpdate, AntennaParams, Position, MODEM_PRESETS
from meshmonitor.api.websocket import notify_node_updated

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


# ---------------------------------------------------------------------------
# Schemi risposta
# ---------------------------------------------------------------------------

class NodeResponse(BaseModel):
    id: str
    short_name: Optional[str]
    long_name: Optional[str]
    hardware_model: Optional[str]
    firmware: Optional[str]
    position: Optional[Position]
    ground_height_m: Optional[float]
    frequency_mhz: Optional[int]
    modem_preset: Optional[str]
    antenna: Optional[AntennaParams]
    last_seen: Optional[datetime]
    notes: Optional[str]
    is_complete: bool
    erp_warning: Optional[bool] = None

    @classmethod
    def from_node(cls, n: Node) -> "NodeResponse":
        erp_warn = None
        if n.antenna and n.antenna.tx_power_dbm and n.antenna.gain_dbi:
            from meshmonitor.processing.link_budget import check_erp_warning
            _, erp_warn = check_erp_warning(n.antenna.tx_power_dbm, n.antenna.gain_dbi)
        return cls(
            **n.model_dump(mode="json"),
            is_complete=n.is_complete,
            erp_warning=erp_warn,
        )


class NodeCreateRequest(BaseModel):
    id: str
    short_name: Optional[str] = None
    long_name: Optional[str] = None
    position: Optional[Position] = None
    ground_height_m: Optional[float] = None
    frequency_mhz: Optional[int] = None
    modem_preset: Optional[str] = None
    antenna: Optional[AntennaParams] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=list[NodeResponse])
async def list_nodes(
    frequency: Optional[int] = Query(default=None, description="Filtra per frequenza MHz"),
    preset: Optional[str] = Query(default=None, description="Filtra per modem preset"),
    complete_only: bool = Query(default=False, description="Solo nodi con dati completi"),
):
    """Restituisce la lista di tutti i nodi nel database."""
    nodes = database.load_all()
    result = list(nodes.values())

    if frequency:
        result = [n for n in result if n.frequency_mhz == frequency]
    if preset:
        result = [n for n in result if n.modem_preset == preset]
    if complete_only:
        result = [n for n in result if n.is_complete]

    # Ordina per last_seen decrescente
    result.sort(key=lambda n: n.last_seen or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [NodeResponse.from_node(n) for n in result]


@router.get("/presets")
async def get_presets():
    """Restituisce la lista dei preset modem con i parametri tecnici."""
    return {
        name: {
            **params,
            "name": name,
        }
        for name, params in MODEM_PRESETS.items()
    }


@router.get("/frequencies")
async def get_frequencies():
    """Restituisce le frequenze supportate."""
    return {"frequencies": [433, 868, 915]}


@router.get("/summary")
async def get_summary():
    """Riepilogo stato database."""
    nodes = database.load_all()
    total = len(nodes)
    complete = sum(1 for n in nodes.values() if n.is_complete)

    freqs: dict[str, int] = {}
    presets: dict[str, int] = {}
    for n in nodes.values():
        if n.frequency_mhz:
            freqs[str(n.frequency_mhz)] = freqs.get(str(n.frequency_mhz), 0) + 1
        if n.modem_preset:
            presets[n.modem_preset] = presets.get(n.modem_preset, 0) + 1

    return {
        "total": total,
        "complete": complete,
        "incomplete": total - complete,
        "by_frequency": freqs,
        "by_preset": presets,
    }


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str):
    """Restituisce i dettagli di un nodo specifico."""
    node = database.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")
    return NodeResponse.from_node(node)


@router.post("", response_model=NodeResponse, status_code=201)
async def create_node(req: NodeCreateRequest):
    """Crea un nuovo nodo manualmente."""
    existing = database.get_node(req.id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Nodo {req.id!r} già presente. Usa PUT per aggiornarlo.")

    node = Node(
        id=req.id,
        short_name=req.short_name,
        long_name=req.long_name,
        position=req.position,
        ground_height_m=req.ground_height_m,
        frequency_mhz=req.frequency_mhz,
        modem_preset=req.modem_preset,
        antenna=req.antenna,
        notes=req.notes,
        last_seen=datetime.now(timezone.utc),
    )
    saved = database.upsert_node(node)
    asyncio.create_task(notify_node_updated(node.id))
    return NodeResponse.from_node(saved)


@router.put("/{node_id}", response_model=NodeResponse)
async def update_node(node_id: str, req: NodeCreateRequest):
    """Aggiorna completamente un nodo."""
    node = database.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")

    updated = Node(
        id=node_id,
        short_name=req.short_name,
        long_name=req.long_name,
        position=req.position,
        ground_height_m=req.ground_height_m,
        frequency_mhz=req.frequency_mhz,
        modem_preset=req.modem_preset,
        antenna=req.antenna,
        notes=req.notes,
        last_seen=datetime.now(timezone.utc),
    )
    saved = database.upsert_node(updated)
    asyncio.create_task(notify_node_updated(node_id))
    return NodeResponse.from_node(saved)


@router.patch("/{node_id}", response_model=NodeResponse)
async def patch_node(node_id: str, update: NodeUpdate):
    """
    Aggiorna parzialmente un nodo.
    Solo i campi non-null nel body vengono aggiornati.
    Utile soprattutto per aggiornare i parametri antenna.
    """
    node = database.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")

    # Applica solo i campi presenti nel body
    update_data = update.model_dump(exclude_none=True)
    for field, value in update_data.items():
        object.__setattr__(node, field, value)

    node = database.upsert_node(node)
    asyncio.create_task(notify_node_updated(node_id))
    return NodeResponse.from_node(node)


@router.delete("/{node_id}", status_code=204)
async def delete_node(node_id: str):
    """Elimina un nodo dal database."""
    deleted = database.delete_node(node_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")


@router.get("/{node_id}/link_budget")
async def get_node_link_budget(node_id: str):
    """
    Calcola e restituisce il link budget per un nodo.
    Include ERP, sensibilità, distanza massima.
    """
    node = database.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail=f"Nodo {node_id!r} non trovato")
    if not node.is_complete:
        raise HTTPException(status_code=422, detail="Dati nodo incompleti per il calcolo del link budget")

    from meshmonitor.processing.link_budget import compute_node_link_budget_summary
    summary = compute_node_link_budget_summary(node)
    return summary
