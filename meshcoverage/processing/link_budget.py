"""
Calcolo link budget per sistemi LoRa/Meshtastic.

Formule utilizzate:
- FSPL (Free Space Path Loss): L_fs = 20*log10(d) + 20*log10(f) + 20*log10(4π/c)
- Link Budget: LB = P_tx + G_tx + G_rx - FSPL - L_atm - L_margin
- ERP: ERP_dBm = P_tx_dBm + G_tx_dBi - 2.15  (rispetto ad antenna isotropica)
"""
from __future__ import annotations
import math
import logging
from typing import Optional

from meshmonitor.models.node import MODEM_PRESETS, Node

log = logging.getLogger(__name__)

# Perdite atmosferiche standard per LoRa (dB/km)
# Valori tipici per frequenze sub-GHz in condizioni standard
ATMOSPHERIC_LOSS_DB_PER_KM = {
    433: 0.006,   # dB/km a 433 MHz
    868: 0.012,   # dB/km a 868 MHz
    915: 0.013,   # dB/km a 915 MHz
}

# Velocità della luce (m/s)
C = 299_792_458.0


def freq_mhz_to_hz(freq_mhz: float) -> float:
    return freq_mhz * 1e6


def fspl_db(distance_m: float, freq_mhz: float) -> float:
    """
    Free Space Path Loss in dB.
    FSPL = 20*log10(d_m) + 20*log10(f_hz) + 20*log10(4π/c)
    """
    if distance_m <= 0:
        return 0.0
    f_hz = freq_mhz_to_hz(freq_mhz)
    return (
        20 * math.log10(distance_m) +
        20 * math.log10(f_hz) +
        20 * math.log10(4 * math.pi / C)
    )


def atmospheric_loss_db(distance_m: float, freq_mhz: int) -> float:
    """Perdite atmosferiche per distanza e frequenza."""
    loss_per_km = ATMOSPHERIC_LOSS_DB_PER_KM.get(freq_mhz, 0.01)
    return loss_per_km * distance_m / 1000.0


def calculate_erp(tx_power_dbm: float, antenna_gain_dbi: float) -> float:
    """
    ERP (Effective Radiated Power) in dBm.
    ERP = P_tx + G_tx (riferito ad antenna isotropica)
    """
    return tx_power_dbm + antenna_gain_dbi


def calculate_link_budget(
    distance_m: float,
    freq_mhz: int,
    modem_preset: str,
    tx_power_dbm: float,
    tx_gain_dbi: float,
    rx_gain_dbi: float = 2.15,
    additional_loss_db: float = 0.0,
) -> dict:
    """
    Calcola il link budget completo.

    Returns:
        dict con:
        - rx_power_dbm: potenza ricevuta (dBm)
        - sensitivity_dbm: sensibilità ricevitore
        - link_margin_db: margine link (rx_power - sensitivity)
        - fspl_db: perdita spazio libero
        - atm_loss_db: perdite atmosferiche
        - erp_dbm: potenza irradiata effettiva
        - reachable: True se link_margin >= 0
    """
    preset_data = MODEM_PRESETS.get(modem_preset)
    if not preset_data:
        raise ValueError(f"Preset sconosciuto: {modem_preset}")

    sensitivity = preset_data["receiver_sensitivity_dbm"]
    fspl = fspl_db(distance_m, freq_mhz)
    atm = atmospheric_loss_db(distance_m, freq_mhz)
    erp = calculate_erp(tx_power_dbm, tx_gain_dbi)

    rx_power = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - fspl - atm - additional_loss_db
    margin = rx_power - sensitivity

    return {
        "rx_power_dbm": round(rx_power, 2),
        "sensitivity_dbm": sensitivity,
        "link_margin_db": round(margin, 2),
        "fspl_db": round(fspl, 2),
        "atm_loss_db": round(atm, 3),
        "erp_dbm": round(erp, 2),
        "reachable": margin >= 0,
    }


def max_range_km(
    freq_mhz: int,
    modem_preset: str,
    tx_power_dbm: float,
    tx_gain_dbi: float,
    rx_gain_dbi: float = 2.15,
    margin_db: float = 0.0,
) -> float:
    """
    Calcola la distanza massima teorica in km in condizioni di LOS ideale.
    Usa ricerca binaria.
    """
    preset_data = MODEM_PRESETS.get(modem_preset)
    if not preset_data:
        return 0.0

    sensitivity = preset_data["receiver_sensitivity_dbm"]
    # Budget disponibile per FSPL + perdite
    available = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - sensitivity - margin_db

    # Stima iniziale: FSPL = 20*log10(d) + 20*log10(f) + K
    # Ignoriamo perdite atm per la stima iniziale (sono piccole a queste freq)
    f_hz = freq_mhz_to_hz(freq_mhz)
    K = 20 * math.log10(f_hz) + 20 * math.log10(4 * math.pi / C)

    # Risolvi per d: available = 20*log10(d) + K → d = 10^((available-K)/20)
    try:
        d_m = 10 ** ((available - K) / 20)
    except Exception:
        return 0.0

    # Affina con ricerca binaria considerando perdite atmosferiche
    lo, hi = 1.0, d_m * 2  # ricerca tra 1m e 2x stima
    for _ in range(50):
        mid = (lo + hi) / 2
        lb = calculate_link_budget(
            mid, freq_mhz, modem_preset, tx_power_dbm, tx_gain_dbi, rx_gain_dbi
        )
        if lb["link_margin_db"] >= margin_db:
            lo = mid
        else:
            hi = mid
        if hi - lo < 10:
            break

    return round(lo / 1000.0, 2)


def check_erp_warning(tx_power_dbm: float, antenna_gain_dbi: float) -> tuple[float, bool]:
    """
    Controlla se l'ERP supera il limite legale di +27 dBm (EU).
    Returns: (erp_dbm, warning)
    """
    from meshmonitor.config import settings
    erp = calculate_erp(tx_power_dbm, antenna_gain_dbi)
    warning = erp > settings.erp_warning_dbm
    return erp, warning


def compute_node_link_budget_summary(node: Node) -> Optional[dict]:
    """
    Calcola il riepilogo del link budget per un nodo.
    Returns None se i dati non sono completi.
    """
    if not node.is_complete:
        return None

    ant = node.antenna
    preset_data = MODEM_PRESETS.get(node.modem_preset)
    if not preset_data:
        return None

    erp, erp_warning = check_erp_warning(ant.tx_power_dbm, ant.gain_dbi)
    max_range = max_range_km(
        node.frequency_mhz, node.modem_preset,
        ant.tx_power_dbm, ant.gain_dbi
    )

    return {
        "node_id": node.id,
        "frequency_mhz": node.frequency_mhz,
        "modem_preset": node.modem_preset,
        "tx_power_dbm": ant.tx_power_dbm,
        "antenna_gain_dbi": ant.gain_dbi,
        "erp_dbm": round(erp, 2),
        "erp_warning": erp_warning,
        "sensitivity_dbm": preset_data["receiver_sensitivity_dbm"],
        "max_range_km": max_range,
        "modem_description": preset_data["description"],
    }
