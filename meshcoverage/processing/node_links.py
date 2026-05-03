"""
Calculates direct connections between nodes.

For each pair of nodes with the same frequency and modem preset:
1. Verifies LOS and Fresnel zone via DEM profile
2. Calculates bidirectional link margin
3. Saves results per freq+preset in JSON

NOTE ON NAMING:
  calculate_link_budget() returns 'link_margin_db' — the margin above the
  receiver sensitivity threshold (positive = reachable, negative = too weak).
  All fields in the output JSON use the 'link_margin' prefix to match this.
  The old 'link_budget' naming was a misnomer: link budget is the total
  available signal budget, whereas link margin is what remains after all losses.
"""
from __future__ import annotations
import json
import logging
import math
from datetime import datetime, timezone
from itertools import combinations
from collections import defaultdict

from meshcoverage.config import settings
from meshcoverage import database
from meshcoverage.models.node import Node
from meshcoverage.processing.dem_handler import get_dem_handler, haversine_m, bearing_deg
from meshcoverage.processing.fresnel import check_los, check_fresnel_clearance
from meshcoverage.processing.link_budget import calculate_link_budget

log = logging.getLogger(__name__)


def _compute_link(node_a: Node, node_b: Node, dem) -> dict | None:
    """
    Calculates the direct connection between two nodes.
    Returns None if LOS is not available or data is insufficient.
    """
    if not node_a.position or not node_b.position:
        return None

    dist_m = haversine_m(
        node_a.position.lat, node_a.position.lon,
        node_b.position.lat, node_b.position.lon,
    )

    if dist_m < 10:
        return None

    # Antenna altitudes
    elev_a = dem.get_elevation(node_a.position.lat, node_a.position.lon)
    elev_b = dem.get_elevation(node_b.position.lat, node_b.position.lon)

    if elev_a is None or elev_b is None:
        log.debug(f"No DEM data for {node_a.id} or {node_b.id}, skipping link")
        return None

    alt_a = elev_a + (node_a.ground_height_m or 3.0)
    alt_b = elev_b + (node_b.ground_height_m or 3.0)

    # Elevation profile
    n_samples = max(50, int(dist_m / 30))
    distances_m, lats, elevations = dem.get_profile(
        node_a.position.lat, node_a.position.lon,
        node_b.position.lat, node_b.position.lon,
        n_samples,
    )

    import numpy as np
    from meshcoverage.processing.dem_handler import earth_bulge_m
    elevations_corr = np.where(
        np.isnan(elevations),
        np.nan,
        elevations + np.array([earth_bulge_m(d) for d in distances_m]),
    )

    # LOS check
    los_ok, _ = check_los(distances_m, elevations_corr, alt_a, alt_b, dist_m, apply_earth_bulge=False)
    if not los_ok:
        return None

    # Fresnel zone check — returns a bool, stored as bool (not str)
    fresnel_ok, _ = check_fresnel_clearance(
        distances_m, elevations_corr,
        alt_a, alt_b, dist_m,
        node_a.frequency_mhz,
    )

    # Bearing A→B and B→A
    brng_a_to_b = bearing_deg(
        node_a.position.lat, node_a.position.lon,
        node_b.position.lat, node_b.position.lon,
    )
    brng_b_to_a = (brng_a_to_b + 180) % 360

    # Antenna gain of A towards B, and B towards A
    gain_a = node_a.antenna.gain_at_azimuth(brng_a_to_b) if node_a.antenna else 0.0
    gain_b = node_b.antenna.gain_at_azimuth(brng_b_to_a) if node_b.antenna else 0.0

    # Verify that B is within A's coverage sector (and vice versa)
    if node_a.antenna and not node_a.antenna.is_in_coverage_sector(brng_a_to_b):
        return None
    if node_b.antenna and not node_b.antenna.is_in_coverage_sector(brng_b_to_a):
        return None

    # When Fresnel zone is partially obstructed, apply a 6 dB diffraction penalty
    diffraction_loss = 0.0 if fresnel_ok else 6.0

    # TX powers
    tx_a = node_a.antenna.tx_power_dbm if node_a.antenna else 20.0
    tx_b = node_b.antenna.tx_power_dbm if node_b.antenna else 20.0

    # Link margin A→B: margin remaining above receiver sensitivity threshold (dB)
    lb_a_to_b = calculate_link_budget(
        dist_m, node_a.frequency_mhz, node_a.modem_preset,
        tx_a, gain_a, gain_b,
        additional_loss_db=diffraction_loss,
    )
    # Link margin B→A
    lb_b_to_a = calculate_link_budget(
        dist_m, node_b.frequency_mhz, node_b.modem_preset,
        tx_b, gain_b, gain_a,
        additional_loss_db=diffraction_loss,
    )

    # The effective link quality is limited by the weaker direction
    min_margin = min(lb_a_to_b["link_margin_db"], lb_b_to_a["link_margin_db"])

    return {
        "node_a_id": node_a.id,
        "node_b_id": node_b.id,
        "distance_km": round(dist_m / 1000, 3),
        "los": True,                         # Only reached here when LOS is confirmed
        "fresnel_ok": bool(fresnel_ok),      # Stored as bool, not str
        # link_margin_* = margin above receiver sensitivity (dB); positive means reachable
        "link_margin_a_to_b": lb_a_to_b["link_margin_db"],
        "link_margin_b_to_a": lb_b_to_a["link_margin_db"],
        "min_link_margin": round(min_margin, 2),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_node_links():
    """
    Calculates all direct connections between nodes.
    Grouped by (frequency, preset) to filter incompatible pairs.
    """
    settings.links_dir.mkdir(parents=True, exist_ok=True)
    dem = get_dem_handler()
    nodes = database.get_complete_nodes()

    if len(nodes) < 2:
        log.info("Fewer than 2 complete nodes, no links to calculate")
        return

    # Group by freq+preset — only nodes on the same channel can communicate
    groups: dict[tuple, list[Node]] = defaultdict(list)
    for node in nodes:
        groups[(node.frequency_mhz, node.modem_preset)].append(node)

    total_links = 0
    for (freq, preset), group_nodes in groups.items():
        if len(group_nodes) < 2:
            continue

        log.info(f"Links {freq}MHz/{preset}: analysing {len(group_nodes)} nodes "
                 f"({len(group_nodes)*(len(group_nodes)-1)//2} pairs)")

        links = []
        pairs = list(combinations(group_nodes, 2))

        for node_a, node_b in pairs:
            try:
                # Skip pairs that are further apart than the configured max range
                if node_a.position and node_b.position:
                    d = haversine_m(
                        node_a.position.lat, node_a.position.lon,
                        node_b.position.lat, node_b.position.lon,
                    )
                    if d > settings.max_range_km * 1000:
                        continue

                link = _compute_link(node_a, node_b, dem)
                if link:
                    links.append(link)
            except Exception as e:
                log.debug(f"Error computing link {node_a.id}↔{node_b.id}: {e}")

        # Sort by descending link margin — best connections first
        links.sort(key=lambda l: l["min_link_margin"], reverse=True)

        # Save results
        out_path = settings.links_dir / f"links_{freq}_{preset}.json"
        with open(out_path, "w") as f:
            json.dump({
                "frequency_mhz": freq,
                "modem_preset": preset,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "node_count": len(group_nodes),
                "link_count": len(links),
                "links": links,
            }, f, indent=2)

        log.info(f"✓ Links {freq}MHz/{preset}: {len(links)} connections found")
        total_links += len(links)

    log.info(f"Link calculation complete: {total_links} total connections")
