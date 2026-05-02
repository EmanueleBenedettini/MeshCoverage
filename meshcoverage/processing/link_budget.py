"""
Calcolo link budget per sistemi LoRa/Meshtastic.

Formule utilizzate (Friis Transmission Equation):
  FSPL (dB) = 20·log10(d_m) + 20·log10(f_hz) + 20·log10(4π/c)
  P_rx (dBm) = P_tx + G_tx + G_rx - FSPL - L_atm
  Link margin (dB) = P_rx - Sensitivity

NOTE: fspl_db() returns PURE path loss. Antenna gains are NOT included
inside it — they are applied separately in calculate_link_budget() via
the standard Friis formula. This avoids the double-counting bug where
gains were both subtracted inside fspl_db() and added back in the caller.
"""
from __future__ import annotations
import math
import logging
from typing import Optional

from meshcoverage.models.node import MODEM_PRESETS, Node

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Atmospheric absorption (ITU-R P.676-12, sea level, standard atmosphere)
# For sub-GHz LoRa bands, gaseous absorption is dominated by O2 and is
# essentially flat and very small across 400–1000 MHz.
# ---------------------------------------------------------------------------
ATMOSPHERIC_LOSS_DB_PER_KM = {
    433: 0.003,   # dB/km
    868: 0.003,   # dB/km
    915: 0.003,   # dB/km
}

# Speed of light (m/s)
C = 299_792_458.0


def fspl_db(distance_m: float, freq_mhz: float) -> float:
    """
    Free Space Path Loss in dB (pure path loss, no antenna gains).

    FSPL = 20·log10(d_m) + 20·log10(f_hz) + 20·log10(4π/c)

    Antenna gains are applied separately in calculate_link_budget()
    following the standard Friis transmission equation:
        P_rx = P_tx + G_tx + G_rx - FSPL - losses
    """
    if distance_m <= 0:
        return 0.0
    f_hz = freq_mhz * 1e6
    return (
        20 * math.log10(distance_m) +
        20 * math.log10(f_hz) +
        20 * math.log10(4 * math.pi / C)
    )


def atmospheric_loss_db(distance_m: float, freq_mhz: int) -> float:
    """Atmospheric absorption loss (ITU-R P.676-12)."""
    loss_per_km = ATMOSPHERIC_LOSS_DB_PER_KM.get(freq_mhz, 0.003)
    return loss_per_km * distance_m / 1000.0


def calculate_erp(tx_power_dbm: float, antenna_gain_dbi: float) -> float:
    """
    ERP (Effective Radiated Power) in dBm referenced to isotropic.
    ERP = P_tx + G_tx
    """
    return tx_power_dbm + antenna_gain_dbi


def calculate_link_budget(
    distance_m: float,
    freq_mhz: int,
    modem_preset: str,
    tx_power_dbm: float = 20.0,
    tx_gain_dbi: float = 2.15,
    rx_gain_dbi: float = 2.15,
    additional_loss_db: float = 0.0,
) -> dict:
    """
    Calcola il link budget completo (Friis Transmission Equation).

    P_rx = P_tx + G_tx + G_rx - FSPL - L_atm - L_extra
    Link margin = P_rx - Sensitivity

    Args:
        distance_m:        TX-RX distance (m)
        freq_mhz:          Carrier frequency (MHz)
        modem_preset:      Meshtastic modem preset name
        tx_power_dbm:      Transmitter output power (dBm)
        tx_gain_dbi:       TX antenna gain (dBi) — directional gain at target bearing
        rx_gain_dbi:       RX antenna gain (dBi) — default dipole 2.15 dBi
        additional_loss_db: Extra losses (diffraction, cable, etc.)

    Returns dict with:
        rx_power_dbm:    Received power (dBm)
        sensitivity_dbm: Receiver sensitivity threshold
        link_margin_db:  Margin above sensitivity (positive = reachable)
        fspl_db:         Free-space path loss
        atm_loss_db:     Atmospheric absorption
        erp_dbm:         Effective radiated power
        reachable:       True if link_margin >= 0
    """
    preset_data = MODEM_PRESETS.get(modem_preset)
    if not preset_data:
        raise ValueError(f"Preset sconosciuto: {modem_preset}")

    sensitivity = preset_data["receiver_sensitivity_dbm"]
    path_loss = fspl_db(distance_m, freq_mhz)
    atm = atmospheric_loss_db(distance_m, freq_mhz)
    erp = calculate_erp(tx_power_dbm, tx_gain_dbi)

    # Friis: P_rx = P_tx + G_tx + G_rx - FSPL - L_atm - L_extra
    rx_power = (tx_power_dbm + tx_gain_dbi + rx_gain_dbi
                - path_loss - atm - additional_loss_db)
    margin = rx_power - sensitivity

    return {
        "rx_power_dbm": round(rx_power, 2),
        "sensitivity_dbm": sensitivity,
        "link_margin_db": round(margin, 2),
        "fspl_db": round(path_loss, 2),
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
    Calcola la distanza massima teorica (km) in LOS ideale.

    Uses binary search on the correct Friis formula.
    Returns the distance at which link_margin drops to margin_db.
    """
    preset_data = MODEM_PRESETS.get(modem_preset)
    if not preset_data:
        return 0.0

    sensitivity = preset_data["receiver_sensitivity_dbm"]
    # Total available budget above sensitivity
    available = tx_power_dbm + tx_gain_dbi + rx_gain_dbi - sensitivity - margin_db

    # Direct analytical estimate (ignoring small atmospheric term):
    # FSPL = available → 20·log10(d·f·4π/c) = available
    f_hz = freq_mhz * 1e6
    K = 20 * math.log10(f_hz) + 20 * math.log10(4 * math.pi / C)
    try:
        d_m_est = 10 ** ((available - K) / 20)
    except Exception:
        return 0.0

    # Refine with binary search to account for atmospheric loss
    lo, hi = 1.0, d_m_est * 1.5
    for _ in range(60):
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
    Controlla se l'ERP supera il limite di +27 dBm (EU ETSI EN 300-220).
    Returns: (erp_dbm, warning_bool)
    """
    from meshcoverage.config import settings
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
