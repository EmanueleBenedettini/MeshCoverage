"""
Calcolo zona di Fresnel per determinare se una connessione radio
è possibile, tenendo conto degli ostacoli lungo il percorso.

La prima zona di Fresnel deve essere libera almeno al 60% per
garantire una buona propagazione del segnale.
"""
from __future__ import annotations
import math
import numpy as np

# Velocità della luce
C = 299_792_458.0
# Percentuale minima di clearance della prima zona di Fresnel
FRESNEL_CLEARANCE_FRACTION = 0.6


def wavelength_m(freq_mhz: float) -> float:
    """Lunghezza d'onda in metri."""
    return C / (freq_mhz * 1e6)


def fresnel_radius_m(
    d1_m: float, d2_m: float, freq_mhz: float, n: int = 1
) -> float:
    """
    Raggio dell'n-esima zona di Fresnel al punto P che divide
    il percorso in d1 (dal TX) e d2 (al RX).

    Formula: r_n = sqrt(n * λ * d1 * d2 / (d1 + d2))
    """
    lam = wavelength_m(freq_mhz)
    d_total = d1_m + d2_m
    if d_total <= 0 or d1_m <= 0 or d2_m <= 0:
        return 0.0
    return math.sqrt(n * lam * d1_m * d2_m / d_total)


def required_clearance_m(d1_m: float, d2_m: float, freq_mhz: float) -> float:
    """
    Clearance minima richiesta (60% del raggio della prima zona di Fresnel).
    """
    return FRESNEL_CLEARANCE_FRACTION * fresnel_radius_m(d1_m, d2_m, freq_mhz)


def check_fresnel_clearance(
    profile_distances_m: np.ndarray,
    profile_elevations: np.ndarray,
    tx_height_m: float,    # altitudine assoluta TX (m slm)
    rx_height_m: float,    # altitudine assoluta RX (m slm)
    total_distance_m: float,
    freq_mhz: float,
) -> tuple[bool, float]:
    """
    Verifica se la linea di vista ha sufficiente clearance della
    prima zona di Fresnel lungo tutto il percorso.

    La LOS è la linea retta tra TX e RX in altitudine.
    Per ogni punto intermedio si verifica che il terreno sia
    sotto la LOS meno il raggio di Fresnel richiesto.

    Args:
        profile_distances_m: distanze progressive dal TX (m)
        profile_elevations: elevazioni terreno (m slm), NaN dove mancanti
        tx_height_m: altitudine assoluta TX
        rx_height_m: altitudine assoluta RX
        total_distance_m: distanza totale TX-RX
        freq_mhz: frequenza

    Returns:
        (clearance_ok, min_clearance_m):
        - clearance_ok: True se LOS+Fresnel è libera
        - min_clearance_m: minima clearance trovata (può essere negativa = ostacolo)
    """
    if total_distance_m <= 0 or len(profile_distances_m) == 0:
        return True, float("inf")

    min_clearance = float("inf")

    for i, (d1, elev) in enumerate(zip(profile_distances_m, profile_elevations)):
        if np.isnan(elev):
            continue

        d2 = total_distance_m - d1
        if d1 <= 0 or d2 <= 0:
            continue

        # Altezza LOS in questo punto (interpolazione lineare)
        los_height = tx_height_m + (rx_height_m - tx_height_m) * (d1 / total_distance_m)

        # Raggio Fresnel richiesto in questo punto
        f_radius = required_clearance_m(d1, d2, freq_mhz)

        # Clearance = altezza LOS - raggio Fresnel - altezza terreno
        clearance = los_height - f_radius - elev

        min_clearance = min(min_clearance, clearance)

    if min_clearance == float("inf"):
        return True, float("inf")

    return min_clearance >= 0, round(min_clearance, 2)


def check_los(
    profile_distances_m: np.ndarray,
    profile_elevations: np.ndarray,
    tx_height_m: float,
    rx_height_m: float,
    total_distance_m: float,
    apply_earth_bulge: bool = True,
) -> tuple[bool, float]:
    """
    Verifica la linea di vista pura (senza Fresnel), con correzione
    per il rigonfiamento terrestre.

    Returns:
        (los_ok, min_clearance_m)
    """
    if total_distance_m <= 0 or len(profile_distances_m) == 0:
        return True, float("inf")

    from meshcoverage.processing.dem_handler import earth_bulge_m
    min_clearance = float("inf")

    for d1, elev in zip(profile_distances_m, profile_elevations):
        if np.isnan(elev):
            continue
        if d1 <= 0 or d1 >= total_distance_m:
            continue

        # Altezza LOS
        los_height = tx_height_m + (rx_height_m - tx_height_m) * (d1 / total_distance_m)

        # Correzione rigonfiamento terrestre
        bulge = earth_bulge_m(d1) if apply_earth_bulge else 0.0

        clearance = los_height - (elev + bulge)
        min_clearance = min(min_clearance, clearance)

    if min_clearance == float("inf"):
        return True, float("inf")

    return min_clearance >= 0, round(min_clearance, 2)
