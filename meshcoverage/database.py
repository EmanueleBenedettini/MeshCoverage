"""
Node JSON database management.
Thread-safe with lock for concurrent access.
"""
from __future__ import annotations
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from meshcoverage.config import settings
from meshcoverage.models.node import Node, AntennaParams, Position

log = logging.getLogger(__name__)
_lock = threading.RLock()


def _load_raw() -> dict:
    """Loads the raw JSON file."""
    p = settings.nodes_file
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Errore lettura {p}: {e}")
        return {}


def _save_raw(data: dict):
    """Saves the dictionary to the JSON file."""
    p = settings.nodes_file
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    tmp.replace(p)


def _node_to_dict(node: Node) -> dict:
    d = node.model_dump(mode="json")
    # Convert datetime to ISO string
    if d.get("last_seen"):
        ls = d["last_seen"]
        if isinstance(ls, datetime):
            d["last_seen"] = ls.isoformat()
    return d


def _dict_to_node(d: dict) -> Optional[Node]:
    try:
        return Node.model_validate(d)
    except Exception as e:
        log.warning(f"Invalid node in database: {e} — {d.get('id', '?')}")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_all() -> dict[str, Node]:
    """Returns all nodes from the database. Key = node_id."""
    with _lock:
        raw = _load_raw()
        nodes = {}
        for node_id, data in raw.items():
            n = _dict_to_node(data)
            if n:
                nodes[node_id] = n
        return nodes


def save_all(nodes: dict[str, Node]):
    """Saves the complete dictionary of nodes."""
    with _lock:
        raw = {nid: _node_to_dict(n) for nid, n in nodes.items()}
        _save_raw(raw)


def get_node(node_id: str) -> Optional[Node]:
    """Retrieves a single node by ID."""
    with _lock:
        raw = _load_raw()
        data = raw.get(node_id.lower())
        if data is None:
            return None
        return _dict_to_node(data)


def upsert_node(node: Node) -> Node:
    """
    Inserts or updates a node in the database.
    If a node with the same ID already exists, merges the data (keeps the most recent).
    """
    with _lock:
        raw = _load_raw()
        existing_data = raw.get(node.id)

        if existing_data:
            existing = _dict_to_node(existing_data)
            if existing:
                existing.update_from(node)
                node = existing

        raw[node.id] = _node_to_dict(node)
        _save_raw(raw)
        log.debug(f"Node upserted: {node.id}")
        return node


def delete_node(node_id: str) -> bool:
    """Deletes a node from the database. Returns True if deleted."""
    with _lock:
        raw = _load_raw()
        if node_id.lower() in raw:
            del raw[node_id.lower()]
            _save_raw(raw)
            log.info(f"Node deleted: {node_id}")
            return True
        return False


def get_complete_nodes() -> list[Node]:
    """Returns only nodes with complete data for calculation."""
    return [n for n in load_all().values() if n.is_complete]


def get_nodes_by_frequency(freq_mhz: int) -> list[Node]:
    """Filters nodes by frequency."""
    return [n for n in load_all().values() if n.frequency_mhz == freq_mhz]


def get_nodes_by_preset(preset: str) -> list[Node]:
    """Filters nodes by modem preset."""
    return [n for n in load_all().values() if n.modem_preset == preset]


def get_nodes_by_freq_and_preset(freq_mhz: int, preset: str) -> list[Node]:
    """Filters nodes by frequency and preset."""
    return [
        n for n in load_all().values()
        if n.frequency_mhz == freq_mhz and n.modem_preset == preset
    ]


def import_from_json(path: Path) -> int:
    """
    Imports nodes from an external JSON file (list or dictionary format).
    Returns the number of nodes imported.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        nodes_data = {d["id"]: d for d in data if "id" in d}
    elif isinstance(data, dict):
        nodes_data = data
    else:
        raise ValueError("Unsupported JSON format")

    count = 0
    for node_id, node_data in nodes_data.items():
        try:
            node = _dict_to_node(node_data)
            if node:
                upsert_node(node)
                count += 1
        except Exception as e:
            log.warning(f"Skip node {node_id}: {e}")

    log.info(f"Imported {count} nodes from {path}")
    return count
