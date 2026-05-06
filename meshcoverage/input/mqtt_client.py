"""
MQTT client for data acquisition from Meshtastic broker.
Connects to an MQTT broker (e.g. mqtt.meshtastic.org) and analyses
received packets to extract node information.
"""
from __future__ import annotations
import json
import logging
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from meshcoverage.config import settings
from meshcoverage import database
from meshcoverage.input.packet_parser import parse_mqtt_packet

log = logging.getLogger(__name__)


class MQTTClient:
    """
    MQTT client for Meshtastic.
    Receives packets from the broker and updates the node database.
    """

    def __init__(
        self,
        broker: str = None,
        port: int = None,
        topic: str = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        tls: bool = False,
        on_node_update: Optional[Callable] = None,
    ):
        self.broker = broker or settings.mqtt_broker
        self.port = port or settings.mqtt_port
        self.topic = topic or settings.mqtt_topic
        self.username = username or settings.mqtt_username
        self.password = password or settings.mqtt_password
        self.tls = tls or settings.mqtt_tls
        self.on_node_update = on_node_update

        self._client: Optional[mqtt.Client] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = 5

        self.stats = {
            "packets_received": 0,
            "nodes_updated": 0,
            "errors": 0,
            "connected": False,
        }

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info(f"MQTT connected to {self.broker}:{self.port}")
            self.stats["connected"] = True
            client.subscribe(self.topic)
            log.info(f"Subscribed to topic: {self.topic}")
        else:
            log.error(f"MQTT connection failed, code: {rc}")
            self.stats["connected"] = False

    def _on_disconnect(self, client, userdata, rc):
        self.stats["connected"] = False
        if rc != 0:
            log.warning(f"MQTT disconnected unexpectedly (rc={rc}), reconnecting...")

    def _on_message(self, client, userdata, msg):
        self.stats["packets_received"] += 1
        try:
            # Meshtastic packets on MQTT can be JSON or binary protobuf
            payload = msg.payload

            # Try JSON parsing first
            try:
                packet = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Binary protobuf packet — requires meshtastic.mesh_pb2
                # for now we log and skip
                log.debug(f"Non-JSON packet on {msg.topic}, skip")
                return

            # Extract nodes from packet
            nodes = parse_mqtt_packet(packet)
            for node in nodes:
                database.upsert_node(node, from_auto_source=True)
                self.stats["nodes_updated"] += 1
                if self.on_node_update:
                    self.on_node_update(node)

            if nodes:
                log.debug(f"[MQTT] {len(nodes)} nodes updated from {msg.topic}")

        except Exception as e:
            self.stats["errors"] += 1
            log.error(f"Error processing MQTT message: {e}", exc_info=True)

    def start(self):
        """Starts the MQTT client in a separate thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="mqtt-client"
        )
        self._thread.start()
        log.info("MQTT thread started")

    def stop(self):
        """Stops the MQTT client."""
        self._running = False
        if self._client:
            self._client.disconnect()
            self._client.loop_stop()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("MQTT client stopped")

    def _run_loop(self):
        while self._running:
            try:
                self._client = mqtt.Client(
                    client_id=f"meshcoverage_{int(time.time())}",
                    clean_session=True,
                )
                self._client.on_connect = self._on_connect
                self._client.on_disconnect = self._on_disconnect
                self._client.on_message = self._on_message

                if self.username:
                    self._client.username_pw_set(self.username, self.password)

                if self.tls:
                    self._client.tls_set()

                self._client.connect(self.broker, self.port, keepalive=60)
                self._client.loop_forever(retry_first_connection=True)

            except Exception as e:
                log.error(f"MQTT loop error: {e}")

            if self._running:
                log.info(f"MQTT reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)

    def is_connected(self) -> bool:
        return self.stats["connected"]
