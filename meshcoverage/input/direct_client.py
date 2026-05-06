"""
Direct connection client to Meshtastic node via TCP (port 4403).
Uses the official Python meshtastic library.
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
    Direct connection to Meshtastic node via TCP.
    Standard port: 4403.
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
        """Callback for packets received from the node."""
        self.stats["packets_received"] += 1
        try:
            nodes = parse_mqtt_packet(packet)
            for node in nodes:
                database.upsert_node(node, from_auto_source=True)
                self.stats["nodes_updated"] += 1
                if self.on_node_update:
                    self.on_node_update(node)
        except Exception as e:
            self.stats["errors"] += 1
            log.error(f"Error processing direct packet: {e}")

    def _sync_node_list(self, interface):
        """
        Synchronises the complete node list from the interface.
        The Meshtastic client maintains all seen nodes in memory.
        """
        try:
            nodes = interface.nodes
            if not nodes:
                return

            count = 0
            for node_id_int, node_data in nodes.items():
                node = parse_meshtastic_api_node(node_data)
                if node:
                    database.upsert_node(node, from_auto_source=True)
                    count += 1
                    if self.on_node_update:
                        self.on_node_update(node)

            log.info(f"Synchronised {count} nodes from local mesh")
        except Exception as e:
            log.error(f"Error synchronising nodes: {e}")

    def start(self):
        """Starts the direct client in a separate thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="direct-client"
        )
        self._thread.start()
        log.info(f"Direct connection thread started ({self.host}:{self.port})")

    def stop(self):
        """Stops the direct client."""
        self._running = False
        if self._interface:
            try:
                self._interface.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Direct client stopped")

    def _run_loop(self):
        while self._running:
            try:
                # Import here to avoid errors if meshtastic is not installed
                import meshtastic
                import meshtastic.tcp_interface
                from pubsub import pub

                log.info(f"Connecting to {self.host}:{self.port}...")
                self._interface = meshtastic.tcp_interface.TCPInterface(
                    hostname=self.host,
                    portNumber=self.port,
                )
                self.stats["connected"] = True
                log.info(f"Connected to Meshtastic node on {self.host}:{self.port}")

                # Synchronise existing nodes
                time.sleep(3)  # wait for init
                self._sync_node_list(self._interface)

                # Subscribe to new packets
                pub.subscribe(self._on_receive, "meshtastic.receive")

                # Keepalive loop — update nodes every 5 minutes
                while self._running:
                    time.sleep(300)
                    self._sync_node_list(self._interface)

            except ImportError:
                log.error(
                    "Library 'meshtastic' not found. "
                    "Install with: pip install meshtastic"
                )
                break

            except Exception as e:
                self.stats["connected"] = False
                log.error(f"Direct connection error: {e}")

            if self._running:
                log.info(f"Reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)

    def is_connected(self) -> bool:
        return self.stats["connected"]
