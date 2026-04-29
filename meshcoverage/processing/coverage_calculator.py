"""
Orchestratore principale del calcolo di copertura.
Eseguito periodicamente (schedulato) o manualmente.
"""
from __future__ import annotations
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import click

from meshcoverage.config import settings
from meshcoverage import database
from meshcoverage.models.node import Node, MODEM_PRESETS
from meshcoverage.processing.dem_handler import get_dem_handler
from meshcoverage.processing.link_budget import (
    compute_node_link_budget_summary, check_erp_warning, max_range_km
)
from meshcoverage.processing.viewshed import (
    ViewshedParams, compute_viewshed, save_viewshed, load_viewshed
)
from meshcoverage.processing.heatmap_generator import generate_heatmaps
from meshcoverage.processing.node_links import compute_node_links

log = logging.getLogger(__name__)


class CoverageCalculator:
    """Calcola la copertura radio per i nodi Meshtastic."""

    def __init__(self):
        self.dem = get_dem_handler()
        self.n_workers = settings.max_workers if settings.max_workers > 0 else None
        self._status: dict = {}

    def _get_coverage_path(self, node_id: str) -> Path:
        safe_id = node_id.lstrip("!").lower()
        return settings.coverage_dir / f"coverage_{safe_id}.npz"

    def _get_metadata_path(self, node_id: str) -> Path:
        safe_id = node_id.lstrip("!").lower()
        return settings.coverage_dir / f"metadata_{safe_id}.json"

    def _load_metadata(self, node_id: str) -> Optional[dict]:
        p = self._get_metadata_path(node_id)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return None

    def _save_metadata(self, node_id: str, meta: dict):
        p = self._get_metadata_path(node_id)
        with open(p, "w") as f:
            json.dump(meta, f, indent=2, default=str)

    def _needs_recompute(self, node: Node) -> bool:
        meta = self._load_metadata(node.id)
        if meta is None:
            return True
        computed_at_str = meta.get("computed_at")
        if computed_at_str and node.last_seen:
            computed_at = datetime.fromisoformat(computed_at_str)
            if node.last_seen > computed_at:
                return True
        coverage_path = self._get_coverage_path(node.id)
        return not coverage_path.exists()

    def compute_node(
        self,
        node: Node,
        force: bool = False,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> dict:
        """
        Calcola la copertura per un singolo nodo.

        Args:
            node: nodo da calcolare
            force: forza ricalcolo anche se già presente
            progress_callback: callable(node_id, done, total) chiamato ogni 100 punti
        Returns:
            dizionario con risultati e metadati
        """
        if not node.is_complete:
            log.warning(f"Nodo {node.id} non completo, skip")
            return {"node_id": node.id, "status": "incomplete"}

        if not force and not self._needs_recompute(node):
            log.info(f"Nodo {node.id} aggiornato, skip (usa --force per forzare)")
            return {"node_id": node.id, "status": "cached"}

        log.info(f"=== Calcolo copertura nodo {node.id} ({node.short_name}) ===")
        start_time = time.time()

        ant = node.antenna
        erp, erp_warning = check_erp_warning(ant.tx_power_dbm, ant.gain_dbi)
        if erp_warning:
            log.warning(
                f"⚠ ATTENZIONE: Nodo {node.id} — ERP={erp:.1f}dBm supera il limite "
                f"di +{settings.erp_warning_dbm}dBm!"
            )

        max_range = max_range_km(
            node.frequency_mhz, node.modem_preset,
            ant.tx_power_dbm, ant.gain_dbi
        )
        max_range_actual = min(max_range, settings.max_range_km)
        log.info(f"Distanza massima teorica: {max_range:.1f}km → uso {max_range_actual:.1f}km")

        ant_elev = self.dem.get_elevation(node.position.lat, node.position.lon)
        if ant_elev is None:
            log.error(f"Nessun dato DEM per nodo {node.id}")
            return {"node_id": node.id, "status": "no_dem"}

        ant_alt_m = ant_elev + (node.ground_height_m or 3.0)
        log.info(f"Altitudine antenna: {ant_alt_m:.1f}m slm")

        gain_min = ant.gain_min_dbi if ant.gain_min_dbi is not None else ant.gain_dbi - 3
        gain_max = ant.gain_max_dbi if ant.gain_max_dbi is not None else ant.gain_dbi

        params = ViewshedParams(
            node_id=node.id,
            ant_lat=node.position.lat,
            ant_lon=node.position.lon,
            ant_alt_m=ant_alt_m,
            ground_height_m=node.ground_height_m or 3.0,
            freq_mhz=node.frequency_mhz,
            modem_preset=node.modem_preset,
            tx_power_dbm=ant.tx_power_dbm,
            ant_gain_dbi=ant.gain_dbi,
            ant_azimuth_deg=ant.azimuth_deg or 0.0,
            ant_beamwidth_deg=ant.beamwidth_deg or 360.0,
            ant_gain_min_dbi=gain_min,
            ant_gain_max_dbi=gain_max,
            rx_gain_dbi=settings.receiver_gain_dbi,
            max_range_m=max_range_actual * 1000,
            dem_dir=settings.dem_dir,
            resolution_m=settings.dem_resolution,
            rx_height_m=settings.receiver_height_m,
        )

        self._status[node.id] = {
            "status": "computing",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "progress": 0,
        }

        def _internal_progress_cb(done: int, total: int):
            pct = int(100 * done / total) if total > 0 else 0
            self._status[node.id]["progress"] = pct
            # Forward to external caller (e.g. API route → WebSocket)
            if progress_callback is not None:
                try:
                    progress_callback(node.id, done, total)
                except Exception:
                    pass

        results = compute_viewshed(
            params,
            n_workers=self.n_workers or 0,
            progress_callback=_internal_progress_cb,
        )

        settings.coverage_dir.mkdir(parents=True, exist_ok=True)
        coverage_path = self._get_coverage_path(node.id)

        metadata = {
            "node_id": node.id,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "frequency_mhz": node.frequency_mhz,
            "modem_preset": node.modem_preset,
            "erp_dbm": round(erp, 2),
            "erp_warning": erp_warning,
            "max_range_km": max_range_actual,
            "sensitivity_dbm": MODEM_PRESETS[node.modem_preset]["receiver_sensitivity_dbm"],
            "ant_alt_m": round(ant_alt_m, 2),
            "point_count": len(results),
            "covered_points": len([
                r for r in results
                if r.get("link_budget_db", float("-inf")) >= settings.min_link_budget_db
            ]),
            "duration_s": round(time.time() - start_time, 1),
        }

        save_viewshed(results, coverage_path, metadata)
        self._save_metadata(node.id, metadata)

        self._status[node.id] = {
            "status": "done",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
        }

        log.info(
            f"✓ Nodo {node.id}: {metadata['point_count']} punti "
            f"({metadata['covered_points']} coperti) in {metadata['duration_s']}s"
        )
        return {"node_id": node.id, "status": "done", "metadata": metadata}

    def compute_all(
        self,
        force: bool = False,
        node_progress_callback: Optional[Callable[[str, int, int], None]] = None,
    ) -> dict:
        """
        Calcola la copertura per tutti i nodi completi.

        Args:
            force: forza ricalcolo anche se già presente
            node_progress_callback: callable(node_id, done, total) per aggiornamenti WS
        """
        settings.ensure_dirs()
        nodes = database.get_complete_nodes()

        if not nodes:
            log.warning("Nessun nodo completo trovato nel database")
            return {"status": "no_nodes", "computed": 0}

        log.info(f"=== Avvio calcolo copertura per {len(nodes)} nodi ===")
        results = []

        for node in nodes:
            try:
                result = self.compute_node(
                    node, force=force, progress_callback=node_progress_callback
                )
                results.append(result)
            except Exception as e:
                log.error(f"Errore calcolo nodo {node.id}: {e}", exc_info=True)
                results.append({"node_id": node.id, "status": "error", "error": str(e)})

        log.info("=== Generazione heatmap aggregate ===")
        try:
            generate_heatmaps()
        except Exception as e:
            log.error(f"Errore generazione heatmap: {e}", exc_info=True)

        log.info("=== Calcolo connessioni inter-nodo ===")
        try:
            compute_node_links()
        except Exception as e:
            log.error(f"Errore calcolo connessioni: {e}", exc_info=True)

        done = len([r for r in results if r.get("status") == "done"])
        log.info(f"=== Calcolo completato: {done}/{len(nodes)} nodi ===")
        return {"status": "done", "total": len(nodes), "computed": done, "results": results}

    def get_status(self, node_id: str = None) -> dict:
        if node_id:
            return self._status.get(node_id, {"status": "idle"})
        return dict(self._status)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--all", "all_nodes", is_flag=True, help="Calcola per tutti i nodi")
@click.option("--node", "node_id", default=None, help="Calcola per nodo specifico")
@click.option("--force", is_flag=True, help="Forza ricalcolo anche se già presente")
@click.option("--no-heatmap", is_flag=True, help="Salta generazione heatmap")
@click.option("--no-links", is_flag=True, help="Salta calcolo connessioni")
def main(all_nodes, node_id, force, no_heatmap, no_links):
    """Calcola la copertura radio dei nodi Meshtastic."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    settings.ensure_dirs()
    calc = CoverageCalculator()

    if node_id:
        node = database.get_node(node_id)
        if not node:
            click.echo(f"Errore: nodo {node_id!r} non trovato nel database", err=True)
            sys.exit(1)
        result = calc.compute_node(node, force=force)
        click.echo(json.dumps(result, indent=2, default=str))
        if not no_heatmap:
            generate_heatmaps()
        if not no_links:
            compute_node_links()

    elif all_nodes:
        result = calc.compute_all(force=force)
        click.echo(json.dumps(
            {k: v for k, v in result.items() if k != "results"},
            indent=2, default=str
        ))

    else:
        click.echo("Specificare --all o --node <id>", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
