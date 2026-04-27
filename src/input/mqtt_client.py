"""
Client MQTT per acquisizione dati da broker Meshtastic.
Si connette a un broker MQTT (es. mqtt.meshtastic.org) e analizza
i pacchetti ricevuti per estrarre informazioni sui nodi.
"""
from __future__ import annotations
import json
import logging
import threading
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from meshmonitor.config import settings
from meshmonitor import database
from meshmonitor.input.packet_parser import parse_mqtt_packet

log = logging.getLogger(__name__)


class MQTTClient:
    """
    Client MQTT per Meshtastic.
    Riceve pacchetti dal broker e aggiorna il database nodi.
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
            log.info(f"MQTT connesso a {self.broker}:{self.port}")
            self.stats["connected"] = True
            client.subscribe(self.topic)
            log.info(f"Sottoscritto a topic: {self.topic}")
        else:
            log.error(f"MQTT connessione fallita, codice: {rc}")
            self.stats["connected"] = False

    def _on_disconnect(self, client, userdata, rc):
        self.stats["connected"] = False
        if rc != 0:
            log.warning(f"MQTT disconnesso inaspettatamente (rc={rc}), riconnessione...")

    def _on_message(self, client, userdata, msg):
        self.stats["packets_received"] += 1
        try:
            # I pacchetti Meshtastic su MQTT possono essere JSON o protobuf binario
            payload = msg.payload

            # Prova prima il parsing JSON
            try:
                packet = json.loads(payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Pacchetto binario protobuf — richiede meshtastic.mesh_pb2
                # per ora lo logghiamo e saltiamo
                log.debug(f"Pacchetto non-JSON su {msg.topic}, skip")
                return

            # Estrai i nodi dal pacchetto
            nodes = parse_mqtt_packet(packet)
            for node in nodes:
                database.upsert_node(node)
                self.stats["nodes_updated"] += 1
                if self.on_node_update:
                    self.on_node_update(node)

            if nodes:
                log.debug(f"[MQTT] {len(nodes)} nodi aggiornati da {msg.topic}")

        except Exception as e:
            self.stats["errors"] += 1
            log.error(f"Errore elaborazione messaggio MQTT: {e}", exc_info=True)

    def start(self):
        """Avvia il client MQTT in un thread separato."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="mqtt-client"
        )
        self._thread.start()
        log.info("Thread MQTT avviato")

    def stop(self):
        """Ferma il client MQTT."""
        self._running = False
        if self._client:
            self._client.disconnect()
            self._client.loop_stop()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Client MQTT fermato")

    def _run_loop(self):
        while self._running:
            try:
                self._client = mqtt.Client(
                    client_id=f"meshmonitor_{int(time.time())}",
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
                log.error(f"Errore loop MQTT: {e}")

            if self._running:
                log.info(f"Riconnessione MQTT in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)

    def is_connected(self) -> bool:
        return self.stats["connected"]
