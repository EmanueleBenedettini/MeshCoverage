"""
Generatore di heatmap aggregate + shadow zone aggregate.

Per ogni combinazione (frequenza, modem_preset) presente nel database:
1. Carica i viewshed di tutti i nodi corrispondenti
2. Per ogni punto della griglia, mantiene solo il link budget massimo
   (copertura) e, separatamente, raccoglie i punti shadow di tutti i nodi
3. Salva copertura come GeoJSON e shadow zone come GeoJSON separato
"""
from __future__ import annotations
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
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
    Genera anche la shadow zone GeoJSON aggregata.
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
    """Genera la heatmap + shadow GeoJSON per una specifica combinazione freq+preset."""

    # Coverage grid: (lat_q, lon_q) → max_link_budget
    coverage_grid: dict[tuple, float] = {}

    # Shadow grid: (lat_q, lon_q) → True
    # A point is kept as shadow only if NO node covers it.
    # We collect all shadow points first, then subtract covered ones.
    shadow_candidates: dict[tuple, float] = {}  # key → min distance across nodes

    nodes_used = 0
    for node in nodes:
        safe_id = node.id.lstrip("!").lower()
        path = settings.coverage_dir / f"coverage_{safe_id}.npz"
        data = load_viewshed(path)

        if data is None:
            log.debug(f"Nessun viewshed per nodo {node.id}, skip")
            continue

        nodes_used += 1

        # --- Coverage points ---
        if len(data["lats"]) > 0:
            mask = data["link_margin_db"] >= settings.min_link_margin_db
            for i in np.where(mask)[0]:
                lat = float(data["lats"][i])
                lon = float(data["lons"][i])
                lm = float(data["link_margin_db"][i])
                key = _grid_key(lat, lon)
                if key not in coverage_grid or coverage_grid[key] < lm:
                    coverage_grid[key] = lm

        # --- Shadow zone points ---
        shadow_lats = data.get("shadow_lats", np.array([]))
        shadow_lons = data.get("shadow_lons", np.array([]))
        shadow_dists = data.get("shadow_distances", np.array([]))

        for i in range(len(shadow_lats)):
            lat = float(shadow_lats[i])
            lon = float(shadow_lons[i])
            dist = float(shadow_dists[i]) if i < len(shadow_dists) else 0.0
            key = _grid_key(lat, lon)
            # Keep the shadow candidate closest to any transmitter
            if key not in shadow_candidates or shadow_candidates[key] > dist:
                shadow_candidates[key] = dist

    if not coverage_grid and not shadow_candidates:
        log.warning(f"Heatmap {freq}/{preset}: nessun punto trovato")
        return

    # Remove shadow candidates that are already covered by some node
    pure_shadow = {
        key: dist
        for key, dist in shadow_candidates.items()
        if key not in coverage_grid
    }

    generated_at = datetime.now(timezone.utc).isoformat()

    # ── Write coverage GeoJSON ──
    if coverage_grid:
        features = [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(lon, 6), round(lat, 6)],
                },
                "properties": {
                    "link_budget_db": round(lm, 2),
                },
            }
            for (lat, lon), lm in coverage_grid.items()
        ]

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

        out_path = settings.heatmaps_dir / f"heatmap_{freq}_{preset}.geojson"
        with open(out_path, "w") as f:
            json.dump(geojson, f, separators=(",", ":"))

        meta_path = settings.heatmaps_dir / f"heatmap_{freq}_{preset}_meta.json"
        with open(meta_path, "w") as f:
            json.dump({
                "frequency_mhz": freq,
                "modem_preset": preset,
                "generated_at": generated_at,
                "node_count": nodes_used,
                "point_count": len(features),
                "shadow_point_count": len(pure_shadow),
                "file_size_kb": round(out_path.stat().st_size / 1024, 1),
            }, f, indent=2)

        log.info(
            f"✓ Heatmap {freq}MHz/{preset}: {len(features)} punti "
            f"da {nodes_used} nodi → {out_path.name}"
        )

    # ── Write shadow GeoJSON ──
    shadow_features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [round(lon, 6), round(lat, 6)],
            },
            "properties": {
                "distance_m": round(dist),
                "shadow": True,
            },
        }
        for (lat, lon), dist in pure_shadow.items()
    ]

    shadow_geojson = {
        "type": "FeatureCollection",
        "features": shadow_features,
        "properties": {
            "frequency_mhz": freq,
            "modem_preset": preset,
            "generated_at": generated_at,
            "node_count": nodes_used,
            "shadow_count": len(shadow_features),
        },
    }

    shadow_path = settings.heatmaps_dir / f"shadows_{freq}_{preset}.geojson"
    with open(shadow_path, "w") as f:
        json.dump(shadow_geojson, f, separators=(",", ":"))

    log.info(
        f"✓ Shadow zones {freq}MHz/{preset}: {len(shadow_features)} punti → {shadow_path.name}"
    )
