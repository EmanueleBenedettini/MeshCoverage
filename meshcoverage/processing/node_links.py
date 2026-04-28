"""
Calcolo connessioni dirette tra nodi.

Per ogni coppia di nodi con stessa frequenza e modem preset:
1. Verifica LOS e zona di Fresnel tramite profilo DEM
2. Calcola link budget bidirezionale
3. Salva risultati per freq+preset in JSON
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
    Calcola la connessione diretta tra due nodi.
    Returns None se LOS non disponibile o dati insufficienti.
    """
    if not node_a.position or not node_b.position:
        return None

    dist_m = haversine_m(
        node_a.position.lat, node_a.position.lon,
        node_b.position.lat, node_b.position.lon,
    )

    if dist_m < 10:
        return None

    # Altitudini antenne
    elev_a = dem.get_elevation(node_a.position.lat, node_a.position.lon)
    elev_b = dem.get_elevation(node_b.position.lat, node_b.position.lon)

    if elev_a is None or elev_b is None:
        log.debug(f"No DEM per {node_a.id} o {node_b.id}, skip link")
        return None

    alt_a = elev_a + (node_a.ground_height_m or 3.0)
    alt_b = elev_b + (node_b.ground_height_m or 3.0)

    # Profilo di elevazione
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

    # Fresnel check
    fresnel_ok, _ = check_fresnel_clearance(
        distances_m, elevations_corr,
        alt_a, alt_b, dist_m,
        node_a.frequency_mhz,
    )

    # Bearing A→B e B→A
    brng_a_to_b = bearing_deg(
        node_a.position.lat, node_a.position.lon,
        node_b.position.lat, node_b.position.lon,
    )
    brng_b_to_a = (brng_a_to_b + 180) % 360

    # Guadagno antenna A verso B
    gain_a = node_a.antenna.gain_at_azimuth(brng_a_to_b) if node_a.antenna else 0.0
    gain_b = node_b.antenna.gain_at_azimuth(brng_b_to_a) if node_b.antenna else 0.0

    # Verifica che il punto B sia nel settore di copertura di A (e viceversa)
    if node_a.antenna and not node_a.antenna.is_in_coverage_sector(brng_a_to_b):
        return None
    if node_b.antenna and not node_b.antenna.is_in_coverage_sector(brng_b_to_a):
        return None

    diffraction_loss = 0.0 if fresnel_ok else 6.0

    # Link budget A→B
    tx_a = node_a.antenna.tx_power_dbm if node_a.antenna else 20.0
    tx_b = node_b.antenna.tx_power_dbm if node_b.antenna else 20.0
    rx_gain = settings.receiver_gain_dbi  # 2.15 dBi default

    lb_a_to_b = calculate_link_budget(
        dist_m, node_a.frequency_mhz, node_a.modem_preset,
        tx_a, gain_a, gain_b,
        additional_loss_db=diffraction_loss,
    )
    lb_b_to_a = calculate_link_budget(
        dist_m, node_b.frequency_mhz, node_b.modem_preset,
        tx_b, gain_b, gain_a,
        additional_loss_db=diffraction_loss,
    )

    min_lb = min(lb_a_to_b["link_margin_db"], lb_b_to_a["link_margin_db"])

    return {
        "node_a_id": node_a.id,
        "node_b_id": node_b.id,
        "distance_km": round(dist_m / 1000, 3),
        "los": True,
        "fresnel_ok": fresnel_ok,
        "link_budget_a_to_b": lb_a_to_b["link_margin_db"],
        "link_budget_b_to_a": lb_b_to_a["link_margin_db"],
        "min_link_budget": round(min_lb, 2),
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }


def compute_node_links():
    """
    Calcola tutte le connessioni dirette tra nodi.
    Raggruppati per (frequenza, preset) per filtrare incompatibili.
    """
    settings.links_dir.mkdir(parents=True, exist_ok=True)
    dem = get_dem_handler()
    nodes = database.get_complete_nodes()

    if len(nodes) < 2:
        log.info("Meno di 2 nodi completi, nessun link da calcolare")
        return

    # Raggruppa per freq+preset
    groups: dict[tuple, list[Node]] = defaultdict(list)
    for node in nodes:
        groups[(node.frequency_mhz, node.modem_preset)].append(node)

    total_links = 0
    for (freq, preset), group_nodes in groups.items():
        if len(group_nodes) < 2:
            continue

        log.info(f"Link {freq}MHz/{preset}: analisi {len(group_nodes)} nodi "
                 f"({len(group_nodes)*(len(group_nodes)-1)//2} coppie)")

        links = []
        pairs = list(combinations(group_nodes, 2))

        for node_a, node_b in pairs:
            try:
                # Salta coppie troppo distanti (oltre max_range configurato)
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
                log.debug(f"Errore link {node_a.id}↔{node_b.id}: {e}")

        # Ordina per link budget decrescente
        links.sort(key=lambda l: l["min_link_budget"], reverse=True)

        # Salva
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

        log.info(f"✓ Links {freq}MHz/{preset}: {len(links)} connessioni trovate")
        total_links += len(links)

    log.info(f"Calcolo links completato: {total_links} connessioni totali")
