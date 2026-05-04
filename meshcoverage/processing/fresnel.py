"""
Verifies if the line of sight has sufficient clearance of the
first Fresnel zone along the entire path.
"""
from __future__ import annotations
import math
import numpy as np
from meshcoverage.processing.dem_handler import K_EARTH_EFFECTIVE_M

C = 299_792_458.0
FRESNEL_CLEARANCE_FRACTION = 0.6


def wavelength_m(freq_mhz: float) -> float:
    """Wavelength in metres."""
    return C / (freq_mhz * 1e6)


def fresnel_radius_m(d1_m: float, d2_m: float, freq_mhz: float, n: int = 1) -> float:
    """Radius of the n-th Fresnel zone."""
    lam = wavelength_m(freq_mhz)
    d_total = d1_m + d2_m
    if d_total <= 0 or d1_m <= 0 or d2_m <= 0:
        return 0.0
    return math.sqrt(n * lam * d1_m * d2_m / d_total)


def required_clearance_m(d1_m: float, d2_m: float, freq_mhz: float) -> float:
    """Minimum clearance required (60% of the first Fresnel zone radius)."""
    return FRESNEL_CLEARANCE_FRACTION * fresnel_radius_m(d1_m, d2_m, freq_mhz)


def check_fresnel_clearance(
    profile_distances_m: np.ndarray,
    profile_elevations: np.ndarray,
    tx_height_m: float,
    rx_height_m: float,
    total_distance_m: float,
    freq_mhz: float,
) -> tuple[bool, float]:
    """
    Verifies if the line of sight has sufficient clearance of the
    first Fresnel zone along the entire path.

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
    if total_distance_m <= 0 or len(profile_distances_m) == 0:
        return True, float("inf")

    min_clearance = float("inf")

    for d1, elev in zip(profile_distances_m, profile_elevations):
        if np.isnan(elev):
            continue

        d2 = total_distance_m - d1
        if d1 <= 0 or d2 <= 0:
            continue

        # LOS height at this point (linear interpolation)
        los_height = tx_height_m + (rx_height_m - tx_height_m) * (d1 / total_distance_m)

        # Required Fresnel clearance at this point
        required = required_clearance_m(d1, d2, freq_mhz)

        # Margin between LOS and terrain + required clearance
        clearance = los_height - float(elev) - required
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
        apply_earth_bulge: if True, applies earth curvature correction

    Returns:
        (los_ok, min_clearance_m):
        - los_ok: True if LOS is clear
        - min_clearance_m: minimum clearance found (can be negative = obstacle)
    """
    if total_distance_m <= 0 or len(profile_distances_m) == 0:
        return True, float("inf")

    min_clearance = float("inf")

    for d1, elev in zip(profile_distances_m, profile_elevations):
        if np.isnan(elev):
            continue

        # LOS height at this point (linear interpolation)
        los_height = tx_height_m + (rx_height_m - tx_height_m) * (d1 / total_distance_m)

        # Earth bulge correction
        bulge = (d1 * (total_distance_m - d1)) / (2.0 * K_EARTH_EFFECTIVE_M) if apply_earth_bulge else 0.0
        clearance = los_height - (float(elev) + bulge)
        min_clearance = min(min_clearance, clearance)

    if min_clearance == float("inf"):
        return True, float("inf")

    return min_clearance >= 0, round(min_clearance, 2)
