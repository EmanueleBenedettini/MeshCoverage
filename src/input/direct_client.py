"""
Client connessione diretta a nodo Meshtastic via TCP (porta 4403).
Usa la libreria Python ufficiale meshtastic.
"""
from __future__ import annotations
import logging
import threading
import time
from typing import Callable, Optional

from meshcoverage.config import settings
from meshcoverage import database
from meshcoverage.input.packet_parser import (
    parse_meshtastic_api_node,
    parse_mqtt_packet,
    node_id_to_hex,
)

log = logging.getLogger(__name__)


class DirectClient:
    """
    Connessione diretta a nodo Meshtastic via TCP.
    Porta standard: 4403.
    """

    def __init__(
        self,
        host: str = None,
        port: int = None,
        on_node_update: Optional[Callable] = None,
    ):
        self.host = host or settings.direct_host
        self.port = port or settings.direct_port
        self.on_node_update = on_node_update

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._interface = None
        self._reconnect_delay = 10

        self.stats = {
            "packets_received": 0,
            "nodes_updated": 0,
            "errors": 0,
            "connected": False,
        }

    def _on_receive(self, packet, interface):
        """Callback per pacchetti ricevuti dal nodo."""
        self.stats["packets_received"] += 1
        try:
            nodes = parse_mqtt_packet(packet)
            for node in nodes:
                database.upsert_node(node)
                self.stats["nodes_updated"] += 1
                if self.on_node_update:
                    self.on_node_update(node)
        except Exception as e:
            self.stats["errors"] += 1
            log.error(f"Errore elaborazione pacchetto diretto: {e}")

    def _sync_node_list(self, interface):
        """
        Sincronizza la lista nodi completa dall'interfaccia.
        Il client Meshtastic mantiene in memoria tutti i nodi visti nella mesh.
        """
        try:
            nodes = interface.nodes
            if not nodes:
                return

            count = 0
            for node_id_int, node_data in nodes.items():
                node = parse_meshtastic_api_node(node_data)
                if node:
                    database.upsert_node(node)
                    count += 1
                    if self.on_node_update:
                        self.on_node_update(node)

            log.info(f"Sincronizzati {count} nodi dalla mesh locale")
        except Exception as e:
            log.error(f"Errore sincronizzazione nodi: {e}")

    def start(self):
        """Avvia il client diretto in un thread separato."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="direct-client"
        )
        self._thread.start()
        log.info(f"Thread connessione diretta avviato ({self.host}:{self.port})")

    def stop(self):
        """Ferma il client diretto."""
        self._running = False
        if self._interface:
            try:
                self._interface.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Client diretto fermato")

    def _run_loop(self):
        while self._running:
            try:
                # Import qui per evitare errori se meshtastic non è installato
                import meshtastic
                import meshtastic.tcp_interface
                from pubsub import pub

                log.info(f"Connessione a {self.host}:{self.port}...")
                self._interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self.host,
                    portNumber=self.port,
                )
                self.stats["connected"] = True
                log.info(f"Connesso a nodo Meshtastic su {self.host}:{self.port}")

                # Sincronizza nodi esistenti
                time.sleep(3)  # attendi init
                self._sync_node_list(self._interface)

                # Sottoscrivi ai nuovi pacchetti
                pub.subscribe(self._on_receive, "meshtastic.receive")

                # Loop di keepalive — aggiorna nodi ogni 5 minuti
                while self._running:
                    time.sleep(300)
                    self._sync_node_list(self._interface)

            except ImportError:
                log.error(
                    "Libreria 'meshtastic' non trovata. "
                    "Installare con: pip install meshtastic"
                )
                break

            except Exception as e:
                self.stats["connected"] = False
                log.error(f"Errore connessione diretta: {e}")

            if self._running:
                log.info(f"Riconnessione in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)

    def is_connected(self) -> bool:
        return self.stats["connected"]
