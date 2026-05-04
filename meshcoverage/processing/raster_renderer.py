"""
Renders a set of (lat, lon, link_budget_db) points into a georeferenced
RGBA PNG suitable for L.imageOverlay.

Colour ramp matches lbToColor() in map.js exactly:
  ≤ −10 dB → dark blue   #1e3a5f
    0  dB  → blue        #1d4ed8
   10  dB  → amber       #f59e0b
   20  dB  → orange      #f97316
  ≥ 30  dB → green       #22c55e
"""
from __future__ import annotations
import base64
import logging
from io import BytesIO
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Must match heatmap_generator.GRID_DEG
GRID_DEG = 0.001
MAX_DIM  = 3000   # maximum image dimension in pixels

_COLOUR_STOPS = np.array([
    [0.00,  30,  58,  95],
    [0.30,  29,  78, 216],
    [0.50, 245, 158,  11],
    [0.70, 249, 115,  22],
    [1.00,  34, 197,  94],
], dtype=np.float32)


def _apply_colormap(norm: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Map normalised [0,1] values to RGB channels (uint8)."""
    r_ch = np.zeros_like(norm, dtype=np.float32)
    g_ch = np.zeros_like(norm, dtype=np.float32)
    b_ch = np.zeros_like(norm, dtype=np.float32)

    for i in range(len(_COLOUR_STOPS) - 1):
        t0, r0, g0, b0 = _COLOUR_STOPS[i]
        t1, r1, g1, b1 = _COLOUR_STOPS[i + 1]
        seg = (norm >= t0) & (norm <= t1)
        f   = np.where(seg, (norm - t0) / max(t1 - t0, 1e-9), 0.0)
        r_ch = np.where(seg, r0 + f * (r1 - r0), r_ch)
        g_ch = np.where(seg, g0 + f * (g1 - g0), g_ch)
        b_ch = np.where(seg, b0 + f * (b1 - b0), b_ch)

    return (
        np.clip(r_ch, 0, 255).astype(np.uint8),
        np.clip(g_ch, 0, 255).astype(np.uint8),
        np.clip(b_ch, 0, 255).astype(np.uint8),
    )


def render_coverage_png(
    lats: np.ndarray,
    lons: np.ndarray,
    link_budgets: np.ndarray,
    grid_deg: float = GRID_DEG,
    sigma_cells: float = 1.8,
) -> Optional[dict]:
    """
    Build a georeferenced PNG from sparse coverage points.

    Args:
        lats, lons:      Point coordinates (1-D arrays, same length).
        link_budgets:    Link margin (dB) per point.
        grid_deg:        Grid cell size in degrees (must match data quantisation).
        sigma_cells:     Gaussian blur radius in grid cells — controls smoothness.

    Returns:
        {
          "image":  "data:image/png;base64,...",
          "bounds": [[lat_min, lon_min], [lat_max, lon_max]],
          "shape":  [n_rows, n_cols],
        }
        or None when the point set is empty.
    """
    from scipy.ndimage import gaussian_filter
    from PIL import Image

    if len(lats) == 0:
        return None

    # --- Snap bounds to the grid and add one-cell padding ---
    lat_min = round(float(lats.min()) / grid_deg) * grid_deg - grid_deg
    lat_max = round(float(lats.max()) / grid_deg) * grid_deg + grid_deg
    lon_min = round(float(lons.min()) / grid_deg) * grid_deg - grid_deg
    lon_max = round(float(lons.max()) / grid_deg) * grid_deg + grid_deg

    n_rows_raw = int(round((lat_max - lat_min) / grid_deg)) + 1
    n_cols_raw = int(round((lon_max - lon_min) / grid_deg)) + 1

    # Scale down if the grid exceeds MAX_DIM
    scale = min(1.0, MAX_DIM / max(n_rows_raw, n_cols_raw, 1))
    n_rows = max(4, int(n_rows_raw * scale))
    n_cols = max(4, int(n_cols_raw * scale))

    # --- Fill grid: keep maximum link budget per cell ---
    grid = np.full((n_rows, n_cols), np.nan, dtype=np.float32)

    r_idx = np.clip(
        np.round((lat_max - lats) / (lat_max - lat_min) * (n_rows - 1)).astype(int),
        0, n_rows - 1,
    )
    c_idx = np.clip(
        np.round((lons - lon_min) / (lon_max - lon_min) * (n_cols - 1)).astype(int),
        0, n_cols - 1,
    )
    for r, c, lb in zip(r_idx, c_idx, link_budgets):
        if np.isnan(grid[r, c]) or grid[r, c] < lb:
            grid[r, c] = float(lb)

    # --- Gaussian smooth ---
    # sigma is in grid-cell units; adjust for scale
    sigma = sigma_cells / scale if scale < 1.0 else sigma_cells

    valid  = (~np.isnan(grid)).astype(np.float32)
    filled = np.where(np.isnan(grid), 0.0, grid)

    blurred = gaussian_filter(filled, sigma=sigma)
    weight  = gaussian_filter(valid,  sigma=sigma)

    with np.errstate(invalid='ignore', divide='ignore'):
        smooth = np.where(weight > 0.05, blurred / weight, np.nan)

    # --- Colour mapping ---
    # Normalise −10 … +30 dB → 0 … 1  (same as lbToColor in map.js)
    norm = np.clip((smooth + 10.0) / 40.0, 0.0, 1.0)
    r_ch, g_ch, b_ch = _apply_colormap(norm)

    # --- Alpha: smooth falloff at edges, 0 outside coverage ---
    w_max = float(weight.max()) if weight.max() > 0 else 1.0
    alpha = np.where(
        weight > 0.05,
        np.clip((weight / w_max) * 185 + 40, 0, 210),
        0,
    ).astype(np.uint8)

    # --- Encode PNG ---
    rgba = np.stack([r_ch, g_ch, b_ch, alpha], axis=-1)
    img  = Image.fromarray(rgba, 'RGBA')
    buf  = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    b64  = base64.b64encode(buf.getvalue()).decode()

    log.debug(
        "render_coverage_png: %d pts → %dx%d px  scale=%.2f  σ=%.1f",
        len(lats), n_cols, n_rows, scale, sigma,
    )

    return {
        "image":  f"data:image/png;base64,{b64}",
        "bounds": [
            [float(lat_min), float(lon_min)],
            [float(lat_max), float(lon_max)],
        ],
        "shape": [n_rows, n_cols],
    }