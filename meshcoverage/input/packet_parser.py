"""
Parser pacchetti Meshtastic.
Estrae informazioni sui nodi dai vari tipi di pacchetto.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

from meshmonitor.models.node import Node, Position, MODEM_PRESETS

log = logging.getLogger(__name__)

# Mapping hardware model number → string
HARDWARE_MODELS = {
    0: "UNSET", 1: "TLORA_V2", 2: "TLORA_V1", 3: "TLORA_V2_1_1P6",
    4: "TBEAM", 5: "HELTEC_V2_0", 6: "TBEAM_V0P7", 7: "T_ECHO",
    8: "TLORA_V1_1P3", 9: "RAK4631", 10: "HELTEC_V2_1", 11: "HELTEC_V1",
    12: "LILYGO_TBEAM_S3_CORE", 13: "RAK11200", 14: "NANO_G1",
    15: "TLORA_4_2_V2_1_S3", 16: "TLORA_T3_S3", 17: "NANO_G1_EXPLORER",
    18: "STATION_G1", 19: "M5STACK_COREINK", 20: "T_DECK",
    21: "UNPHONE", 22: "T_WATCH_S3", 23: "PICOMPUTER_S3",
    24: "HELTEC_HT62", 25: "EBYTE_ESP32_S3", 26: "ESP32_S3_PICO",
    27: "HELTEC_MESH_NODE_T114", 28: "SENSECAP_INDICATOR",
    29: "TRACKER_T1000_E", 30: "RAK3172", 31: "WIO_WM1110",
    255: "PRIVATE_HW",
}

# Mapping preset string → int e viceversa
PRESET_INT_TO_STR = {
    0: "SHORT_TURBO", 1: "SHORT_FAST", 2: "SHORT_SLOW",
    3: "MEDIUM_FAST", 4: "MEDIUM_SLOW", 5: "LONG_FAST",
    6: "LONG_MODERATE", 7: "LONG_SLOW", 8: "VERY_LONG_SLOW",
}
PRESET_STR_TO_INT = {v: k for k, v in PRESET_INT_TO_STR.items()}

# Mapping frequenza canale → MHz
FREQ_CHANNEL_MAPS = {
    "LongFast": 868, "LongSlow": 868, "LongMod": 868,
    "MedFast": 868, "MedSlow": 868,
    "ShortFast": 868, "ShortSlow": 868, "ShortTurbo": 868,
}


def node_id_to_hex(node_num: int) -> str:
    """Converte numero nodo intero in stringa !aabbccdd."""
    return f"!{node_num:08x}"


def parse_node_info(packet: dict) -> Optional[Node]:
    """
    Estrae dati nodo da un pacchetto NodeInfo / portnum=NODEINFO_APP.
    """
    try:
        decoded = packet.get("decoded", {})
        node_info = decoded.get("user", decoded.get("nodeinfo", {}))

        from_num = packet.get("from", 0)
        node_id = node_info.get("id") or node_id_to_hex(from_num)
        if not node_id:
            return None

        return Node(
            id=node_id,
            short_name=node_info.get("shortName") or node_info.get("short_name"),
            long_name=node_info.get("longName") or node_info.get("long_name"),
            hardware_model=HARDWARE_MODELS.get(
                node_info.get("hwModel", 0), node_info.get("hwModel", "UNKNOWN")
            ),
            last_seen=datetime.now(timezone.utc),
        )
    except Exception as e:
        log.debug(f"parse_node_info error: {e}")
        return None


def parse_position(packet: dict) -> Optional[Node]:
    """
    Estrae posizione GPS da un pacchetto Position / portnum=POSITION_APP.
    """
    try:
        decoded = packet.get("decoded", {})
        pos_data = decoded.get("position", {})

        from_num = packet.get("from", 0)
        if not from_num:
            return None

        lat = pos_data.get("latitudeI", pos_data.get("latitude_i", 0)) / 1e7
        lon = pos_data.get("longitudeI", pos_data.get("longitude_i", 0)) / 1e7
        alt = pos_data.get("altitude", None)

        if lat == 0.0 and lon == 0.0:
            return None

        return Node(
            id=node_id_to_hex(from_num),
            position=Position(lat=lat, lon=lon),
            ground_height_m=alt,
            last_seen=datetime.now(timezone.utc),
        )
    except Exception as e:
        log.debug(f"parse_position error: {e}")
        return None


def parse_device_metrics(packet: dict) -> Optional[Node]:
    """
    Estrae metriche dispositivo (batteria, uptime) — limitato ma salviamo last_seen.
    """
    try:
        from_num = packet.get("from", 0)
        if not from_num:
            return None
        return Node(
            id=node_id_to_hex(from_num),
            last_seen=datetime.now(timezone.utc),
        )
    except Exception as e:
        log.debug(f"parse_device_metrics error: {e}")
        return None


def parse_channel_config(packet: dict, node_id: str) -> Optional[Node]:
    """
    Estrae configurazione canale (frequenza, modem preset) se disponibile.
    I pacchetti di configurazione non sono sempre presenti nel flusso MQTT pubblico.
    """
    try:
        decoded = packet.get("decoded", {})
        config = decoded.get("config", {})
        lora = config.get("lora", {})

        if not lora:
            return None

        # Estrai preset
        preset_int = lora.get("modemPreset", lora.get("modem_preset", 3))
        preset_str = PRESET_INT_TO_STR.get(preset_int, "MEDIUM_FAST")

        # Frequenza: usa region o frequenza override
        region = lora.get("region", 0)
        freq_override = lora.get("overrideFrequency", lora.get("override_frequency", 0))

        freq_mhz = None
        if freq_override > 0:
            freq_mhz = int(freq_override / 1e6)
        elif region in (3, 4):  # EU_433, EU_868
            freq_mhz = 433 if region == 3 else 868
        elif region == 7:  # US
            freq_mhz = 915

        return Node(
            id=node_id,
            modem_preset=preset_str,
            frequency_mhz=freq_mhz,
            last_seen=datetime.now(timezone.utc),
        )
    except Exception as e:
        log.debug(f"parse_channel_config error: {e}")
        return None


def parse_mqtt_packet(raw_packet: dict) -> list[Node]:
    """
    Analizza un pacchetto MQTT Meshtastic e restituisce i nodi estratti.
    Un singolo pacchetto può aggiornare più aspetti di un nodo.
    """
    nodes = []
    portnum = raw_packet.get("decoded", {}).get("portnum", "")

    # NodeInfo
    if portnum in ("NODEINFO_APP", 4):
        n = parse_node_info(raw_packet)
        if n:
            nodes.append(n)

    # Position
    if portnum in ("POSITION_APP", 3):
        n = parse_position(raw_packet)
        if n:
            nodes.append(n)

    # Telemetria dispositivo
    if portnum in ("TELEMETRY_APP", 67):
        n = parse_device_metrics(raw_packet)
        if n:
            nodes.append(n)

    # Qualsiasi pacchetto aggiorna last_seen del mittente
    from_num = raw_packet.get("from", 0)
    if from_num and not nodes:
        nodes.append(Node(
            id=node_id_to_hex(from_num),
            last_seen=datetime.now(timezone.utc),
        ))

    return nodes


def parse_meshtastic_api_node(api_node: dict) -> Optional[Node]:
    """
    Parsa un nodo dalla risposta dell'API Python meshtastic
    (mesh_interface.nodes).
    """
    try:
        user = api_node.get("user", {})
        pos = api_node.get("position", {})
        dev_metrics = api_node.get("deviceMetrics", {})

        node_id = user.get("id")
        if not node_id:
            return None

        position = None
        if "latitude" in pos and "longitude" in pos:
            lat = pos["latitude"]
            lon = pos["longitude"]
            if lat != 0 or lon != 0:
                position = Position(lat=lat, lon=lon)

        last_heard = api_node.get("lastHeard")
        last_seen = None
        if last_heard:
            last_seen = datetime.fromtimestamp(last_heard, tz=timezone.utc)

        hw_model = HARDWARE_MODELS.get(
            user.get("hwModel", 0), str(user.get("hwModel", "UNKNOWN"))
        )

        return Node(
            id=node_id,
            short_name=user.get("shortName"),
            long_name=user.get("longName"),
            hardware_model=hw_model,
            position=position,
            ground_height_m=pos.get("altitude"),
            last_seen=last_seen,
        )
    except Exception as e:
        log.warning(f"parse_meshtastic_api_node error: {e}")
        return None
