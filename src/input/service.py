"""
Servizio di acquisizione dati Meshtastic.
Avvia i client MQTT e/o diretto in base alla configurazione.
"""
from __future__ import annotations
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from meshmonitor.config import settings
from meshmonitor.input.mqtt_client import MQTTClient
from meshmonitor.input.direct_client import DirectClient

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("meshmonitor.input.service")


class InputService:
    def __init__(self):
        self.mqtt_client: MQTTClient | None = None
        self.direct_client: DirectClient | None = None
        self._running = False

    def start(self):
        settings.ensure_dirs()
        self._running = True

        if settings.mqtt_enabled:
            self.mqtt_client = MQTTClient()
            self.mqtt_client.start()
            log.info(f"MQTT attivato — broker: {settings.mqtt_broker}:{settings.mqtt_port}")
        else:
            log.info("MQTT disabilitato (MESHMONITOR_MQTT_ENABLED=false)")

        if settings.direct_enabled:
            self.direct_client = DirectClient()
            self.direct_client.start()
            log.info(f"Connessione diretta attivata — {settings.direct_host}:{settings.direct_port}")
        else:
            log.info("Connessione diretta disabilitata (MESHMONITOR_DIRECT_ENABLED=false)")

        if not settings.mqtt_enabled and not settings.direct_enabled:
            log.warning(
                "Nessuna sorgente dati abilitata. "
                "Impostare MESHMONITOR_MQTT_ENABLED=true o MESHMONITOR_DIRECT_ENABLED=true"
            )

    def stop(self):
        self._running = False
        if self.mqtt_client:
            self.mqtt_client.stop()
        if self.direct_client:
            self.direct_client.stop()

    def run_forever(self):
        self.start()
        log.info("Servizio acquisizione dati avviato. Ctrl+C per fermare.")

        def _signal_handler(sig, frame):
            log.info("Signal ricevuto, arresto in corso...")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        while self._running:
            # Log periodico statistiche
            time.sleep(60)
            self._log_stats()

    def _log_stats(self):
        parts = []
        if self.mqtt_client:
            s = self.mqtt_client.stats
            parts.append(
                f"MQTT: pkt={s['packets_received']} nodi={s['nodes_updated']} "
                f"err={s['errors']} conn={'✓' if s['connected'] else '✗'}"
            )
        if self.direct_client:
            s = self.direct_client.stats
            parts.append(
                f"Direct: pkt={s['packets_received']} nodi={s['nodes_updated']} "
                f"err={s['errors']} conn={'✓' if s['connected'] else '✗'}"
            )
        if parts:
            log.info(" | ".join(parts))


def main():
    service = InputService()
    service.run_forever()


if __name__ == "__main__":
    main()
