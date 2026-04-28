"""
Generatore di heatmap aggregate.

Per ogni combinazione (frequenza, modem_preset) presente nel database:
1. Carica i viewshed di tutti i nodi corrispondenti
2. Per ogni punto della griglia, mantiene solo il link budget massimo
3. Salva il risultato come GeoJSON
"""
from __future__ import annotations
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Optional

import numpy as np

from meshcoverage.config import settings
from meshcoverage import database
from meshcoverage.processing.viewshed import load_viewshed

log = logging.getLogger(__name__)

# Risoluzione griglia heatmap in gradi (~100m a latitudine media europea)
GRID_DEG = 0.001  # circa 111m lat, ~80m lon a 45°N


def _grid_key(lat: float, lon: float) -> tuple:
    """Quantizza lat/lon a un bin della griglia heatmap."""
    return (round(lat / GRID_DEG) * GRID_DEG, round(lon / GRID_DEG) * GRID_DEG)


def generate_heatmaps():
    """
    Genera le heatmap GeoJSON per ogni combinazione freq+preset.
    Deve essere chiamata dopo aver calcolato i viewshed dei nodi.
    """
    settings.heatmaps_dir.mkdir(parents=True, exist_ok=True)

    nodes = database.load_all()
    complete_nodes = [n for n in nodes.values() if n.is_complete]

    if not complete_nodes:
        log.warning("Nessun nodo completo per generare heatmap")
        return

    # Raggruppa nodi per (freq, preset)
    groups: dict[tuple, list] = defaultdict(list)
    for node in complete_nodes:
        key = (node.frequency_mhz, node.modem_preset)
        groups[key].append(node)

    for (freq, preset), group_nodes in groups.items():
        log.info(f"Heatmap {freq}MHz / {preset}: {len(group_nodes)} nodi")
        _generate_single_heatmap(freq, preset, group_nodes)

    log.info(f"Heatmap generate: {len(groups)} combinazioni freq/preset")


def _generate_single_heatmap(freq: int, preset: str, nodes: list):
    """Genera la heatmap per una specifica combinazione freq+preset."""

    # Griglia: dict (lat_q, lon_q) → max_link_budget
    grid: dict[tuple, float] = {}

    nodes_used = 0
    for node in nodes:
        safe_id = node.id.lstrip("!").lower()
        path = settings.coverage_dir / f"coverage_{safe_id}.npz"
        data = load_viewshed(path)

        if data is None or len(data["lats"]) == 0:
            log.debug(f"Nessun viewshed per nodo {node.id}, skip")
            continue

        nodes_used += 1

        # Maschera: solo punti con LOS e link budget > minimo
        mask = data["link_budget"] >= settings.min_link_budget_db

        for i in np.where(mask)[0]:
            lat = float(data["lats"][i])
            lon = float(data["lons"][i])
            lb = float(data["link_budget"][i])

            key = _grid_key(lat, lon)
            if key not in grid or grid[key] < lb:
                grid[key] = lb

    if not grid:
        log.warning(f"Heatmap {freq}/{preset}: nessun punto di copertura trovato")
        return

    # Crea GeoJSON
    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 6), round(lat, 6)],
            },
            "properties": {
                "link_budget_db": round(lb, 2),
            },
        }
        for (lat, lon), lb in grid.items()
    ]

    generated_at = datetime.now(timezone.utc).isoformat()

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "frequency_mhz": freq,
            "modem_preset": preset,
            "generated_at": generated_at,
            "node_count": nodes_used,
            "point_count": len(features),
        },
    }

    # Salva GeoJSON
    out_path = settings.heatmaps_dir / f"heatmap_{freq}_{preset}.geojson"
    with open(out_path, "w") as f:
        json.dump(geojson, f, separators=(",", ":"))

    # Salva metadati separati
    meta_path = settings.heatmaps_dir / f"heatmap_{freq}_{preset}_meta.json"
    with open(meta_path, "w") as f:
        json.dump({
            "frequency_mhz": freq,
            "modem_preset": preset,
            "generated_at": generated_at,
            "node_count": nodes_used,
            "point_count": len(features),
            "file_size_kb": round(out_path.stat().st_size / 1024, 1),
        }, f, indent=2)

    log.info(
        f"✓ Heatmap {freq}MHz/{preset}: {len(features)} punti "
        f"da {nodes_used} nodi → {out_path.name}"
    )
