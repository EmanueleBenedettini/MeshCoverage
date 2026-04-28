"""
Verifies if the line of sight has sufficient clearance of the
first Fresnel zone along the entire path.

LOS is the straight line between TX and RX in altitude.
For each intermediate point it is verified that the terrain is
below the LOS minus the required Fresnel radius.

Args:
    profile_distances_m: progressive distances from TX (m)
    profile_elevations: terrain elevations (m slm), NaN where missing
    tx_height_m: absolute TX altitude
    rx_height_m: absolute RX altitude
    total_distance_m: total TX-RX distance
    freq_mhz: frequency

Returns:
    (clearance_ok, min_clearance_m):
    - clearance_ok: True if LOS+Fresnel is clear
    - min_clearance_m: minimum clearance found (can be negative = obstacle)
"""
from __future__ import annotations
import math
import numpy as np

# Speed of light
C = 299_792_458.0
# Minimum clearance percentage of the first Fresnel zone
FRESNEL_CLEARANCE_FRACTION = 0.6


def wavelength_m(freq_mhz: float) -> float:
    """Wavelength in metres."""
    return C / (freq_mhz * 1e6)


def fresnel_radius_m(
    d1_m: float, d2_m: float, freq_mhz: float, n: int = 1
) -> float:
    """
    Radius of the n-th Fresnel zone at point P which divides
    the path into d1 (from TX) and d2 (to RX).

    Formula: r_n = sqrt(n * λ * d1 * d2 / (d1 + d2))
    """
    lam = wavelength_m(freq_mhz)
    d_total = d1_m + d2_m
    if d_total <= 0 or d1_m <= 0 or d2_m <= 0:
        return 0.0
    return math.sqrt(n * lam * d1_m * d2_m / d_total)


def required_clearance_m(d1_m: float, d2_m: float, freq_mhz: float) -> float:
    """
    Minimum clearance required (60% of the radius of the first Fresnel zone).
    """
    return FRESNEL_CLEARANCE_FRACTION * fresnel_radius_m(d1_m, d2_m, freq_mhz)


def check_fresnel_clearance(
    profile_distances_m: np.ndarray,
    profile_elevations: np.ndarray,
    tx_height_m: float,    # absolute TX altitude (m slm)
    rx_height_m: float,    # absolute RX altitude (m slm)
    total_distance_m: float,
    freq_mhz: float,
) -> tuple[bool, float]:
    """
    Verifies if the line of sight has sufficient clearance of the
    first Fresnel zone along the entire path.

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

        # Calculate required Fresnel radius for each point
        required_clearances = required_clearance_m(
            d1_m, d2_m, freq_mhz
        )

        # Verify terrain is below LOS minus Fresnel radius
        clearance = los_height - profile_elevations - required_clearances

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
    Verifies the line of sight between TX and RX.

    Args:
        profile_distances_m: progressive distances from TX (m)
        profile_elevations: terrain elevations (m slm), NaN where missing
        tx_height_m: absolute TX altitude
        rx_height_m: absolute RX altitude
        total_distance_m: total TX-RX distance
        apply_earth_bulge: if True, applies earth curvature

    Returns:
        (los_ok, min_clearance_m):
        - los_ok: True if LOS is clear
        - min_clearance_m: minimum clearance found (can be negative = obstacle)
    """
    if total_distance_m <= 0 or len(profile_distances_m) == 0:
        return True, float("inf")

        # Altezza LOS
        los_height = tx_height_m + (rx_height_m - tx_height_m) * (d1 / total_distance_m)

        # Correzione rigonfiamento terrestre
        bulge = earth_bulge_m(d1) if apply_earth_bulge else 0.0

        clearance = los_height - (elev + bulge)
        min_clearance = min(min_clearance, clearance)

    if min_clearance == float("inf"):
        return True, float("inf")

    return min_clearance >= 0, round(min_clearance, 2)
