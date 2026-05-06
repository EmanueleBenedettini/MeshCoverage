"""
Renders a set of (lat, lon, value) points into a georeferenced
RGBA PNG suitable for L.imageOverlay.

Two public helpers are provided; both delegate to the shared
render_raster_png() core:

  render_coverage_png(lats, lons, link_budgets)
      Colour ramp matches lbToColor() in map.js exactly:
        ≤ −10 dB → dark blue   #1e3a5f
          0  dB  → blue        #1d4ed8
         10  dB  → amber       #f59e0b
         20  dB  → orange      #f97316
        ≥ 30  dB → green       #22c55e

  render_shadow_png(lats, lons)
      Uniform dark-indigo overlay for terrain shadow zones.
      All points are treated as equally blocked; the gaussian
      blur produces a smooth halo at the boundary.
        0.0 → very dark indigo  #140832
        1.0 → medium dark indigo #3d0f78
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

# ── Colour stops — [normalised_t, R, G, B] ────────────────────────────────
#
# Coverage: matches lbToColor() in map.js (do NOT change without updating JS)
_COVERAGE_COLOUR_STOPS = np.array([
    [0.00,  30,  58,  95],
    [0.30,  29,  78, 216],
    [0.50, 245, 158,  11],
    [0.70, 249, 115,  22],
    [1.00,  34, 197,  94],
], dtype=np.float32)

# Shadow zones: dark-indigo, visually distinct from the coverage gradient.
# Kept intentionally dark so it does not compete with the coverage layer
# when both are shown simultaneously.
_SHADOW_COLOUR_STOPS = np.array([
    [0.0, 20,  8,  50],   # very dark indigo  #140832
    [1.0, 61, 15, 120],   # medium dark indigo #3d0f78
], dtype=np.float32)


# ---------------------------------------------------------------------------
# Internal colour-mapping helper
# ---------------------------------------------------------------------------

def _apply_colormap(
    norm: np.ndarray,
    stops: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Map normalised [0, 1] values to RGB channels (uint8) using piecewise
    linear interpolation across the supplied colour stops.

    Args:
        norm:   2-D float32 array, values in [0, 1]
        stops:  N×4 float32 array of [t, R, G, B] breakpoints, sorted by t

    Returns:
        Tuple (R, G, B) of uint8 arrays with the same shape as norm.
    """
    r_ch = np.zeros_like(norm, dtype=np.float32)
    g_ch = np.zeros_like(norm, dtype=np.float32)
    b_ch = np.zeros_like(norm, dtype=np.float32)

    for i in range(len(stops) - 1):
        t0, r0, g0, b0 = stops[i]
        t1, r1, g1, b1 = stops[i + 1]
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


# ---------------------------------------------------------------------------
# Generalised core renderer
# ---------------------------------------------------------------------------

def render_raster_png(
    lats: np.ndarray,
    lons: np.ndarray,
    values: np.ndarray,
    grid_deg: float = GRID_DEG,
    sigma_cells: float = 1.8,
    colour_stops: Optional[np.ndarray] = None,
    vmin: float = -10.0,
    vmax: float = 30.0,
    alpha_min: int = 40,
    alpha_max: int = 210,
) -> Optional[dict]:
    """
    Build a georeferenced RGBA PNG from a sparse set of (lat, lon, value) points.

    This is the shared core used by both render_coverage_png() and
    render_shadow_png().  Callers pass a colour_stops array and a
    normalisation range [vmin, vmax] appropriate for their data.

    Args:
        lats, lons:     Point coordinates (1-D arrays, same length).
        values:         Scalar value per point — will be normalised to [0, 1]
                        via   t = clip((v − vmin) / (vmax − vmin), 0, 1)
        grid_deg:       Grid cell size in degrees (should match data quantisation).
        sigma_cells:    Gaussian blur radius in grid-cell units.
        colour_stops:   N×4 float32 array of [t, R, G, B] stops.
                        Defaults to _COVERAGE_COLOUR_STOPS when None.
        vmin, vmax:     Normalisation bounds for values.
        alpha_min:      Minimum alpha (where weight is near the edge of coverage).
        alpha_max:      Maximum alpha (where weight is densely covered).

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

    if colour_stops is None:
        colour_stops = _COVERAGE_COLOUR_STOPS

    # ── Snap bounds to the grid with one-cell padding ──────────────────────
    lat_min = round(float(lats.min()) / grid_deg) * grid_deg - grid_deg
    lat_max = round(float(lats.max()) / grid_deg) * grid_deg + grid_deg
    lon_min = round(float(lons.min()) / grid_deg) * grid_deg - grid_deg
    lon_max = round(float(lons.max()) / grid_deg) * grid_deg + grid_deg

    n_rows_raw = int(round((lat_max - lat_min) / grid_deg)) + 1
    n_cols_raw = int(round((lon_max - lon_min) / grid_deg)) + 1

    # Scale down if the grid exceeds MAX_DIM
    scale   = min(1.0, MAX_DIM / max(n_rows_raw, n_cols_raw, 1))
    n_rows  = max(4, int(n_rows_raw * scale))
    n_cols  = max(4, int(n_cols_raw * scale))

    # ── Fill grid: keep maximum value per cell ─────────────────────────────
    grid = np.full((n_rows, n_cols), np.nan, dtype=np.float32)

    r_idx = np.clip(
        np.round((lat_max - lats) / (lat_max - lat_min) * (n_rows - 1)).astype(int),
        0, n_rows - 1,
    )
    c_idx = np.clip(
        np.round((lons - lon_min) / (lon_max - lon_min) * (n_cols - 1)).astype(int),
        0, n_cols - 1,
    )
    for r, c, v in zip(r_idx, c_idx, values):
        if np.isnan(grid[r, c]) or grid[r, c] < v:
            grid[r, c] = float(v)

    # ── Gaussian smooth ────────────────────────────────────────────────────
    # sigma is in grid-cell units; adjust for scale-down
    sigma = sigma_cells / scale if scale < 1.0 else sigma_cells

    valid  = (~np.isnan(grid)).astype(np.float32)
    filled = np.where(np.isnan(grid), 0.0, grid)

    blurred = gaussian_filter(filled, sigma=sigma)
    weight  = gaussian_filter(valid,  sigma=sigma)

    with np.errstate(invalid='ignore', divide='ignore'):
        smooth = np.where(weight > 0.05, blurred / weight, np.nan)

    # ── Colour mapping ─────────────────────────────────────────────────────
    # Normalise values into [0, 1] using the caller-supplied range
    if vmax > vmin:
        norm = np.clip((smooth - vmin) / (vmax - vmin), 0.0, 1.0)
    else:
        norm = np.zeros_like(smooth)

    r_ch, g_ch, b_ch = _apply_colormap(norm, colour_stops)

    # ── Alpha: smooth falloff at edges, 0 outside coverage ────────────────
    w_max       = float(weight.max()) if weight.max() > 0 else 1.0
    alpha_range = alpha_max - alpha_min
    alpha = np.where(
        weight > 0.05,
        np.clip((weight / w_max) * alpha_range + alpha_min, 0, 255),
        0,
    ).astype(np.uint8)

    # ── Encode PNG ─────────────────────────────────────────────────────────
    rgba = np.stack([r_ch, g_ch, b_ch, alpha], axis=-1)
    img  = Image.fromarray(rgba, 'RGBA')
    buf  = BytesIO()
    img.save(buf, format='PNG', optimize=True)
    b64  = base64.b64encode(buf.getvalue()).decode()

    log.debug(
        "render_raster_png: %d pts → %dx%d px  scale=%.2f  σ=%.1f"
        "  vmin=%.1f  vmax=%.1f",
        len(lats), n_cols, n_rows, scale, sigma, vmin, vmax,
    )

    return {
        "image":  f"data:image/png;base64,{b64}",
        "bounds": [
            [float(lat_min), float(lon_min)],
            [float(lat_max), float(lon_max)],
        ],
        "shape": [n_rows, n_cols],
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def render_coverage_png(
    lats: np.ndarray,
    lons: np.ndarray,
    link_budgets: np.ndarray,
    grid_deg: float = GRID_DEG,
    sigma_cells: float = 1.8,
) -> Optional[dict]:
    """
    Render link-budget values as a georeferenced RGBA PNG.

    Colour ramp matches lbToColor() in map.js (do NOT change without
    updating the JS counterpart):
      ≤ −10 dB  →  dark blue  #1e3a5f
        0  dB   →  blue       #1d4ed8
       10  dB   →  amber      #f59e0b
       20  dB   →  orange     #f97316
      ≥ 30  dB  →  green      #22c55e
    """
    return render_raster_png(
        lats, lons, link_budgets,
        grid_deg=grid_deg,
        sigma_cells=sigma_cells,
        colour_stops=_COVERAGE_COLOUR_STOPS,
        vmin=-10.0,
        vmax=30.0,
        alpha_min=40,
        alpha_max=210,
    )


def render_shadow_png(
    lats: np.ndarray,
    lons: np.ndarray,
    grid_deg: float = GRID_DEG,
    sigma_cells: float = 2.5,
) -> Optional[dict]:
    """
    Render terrain shadow zones as a georeferenced RGBA PNG.

    All shadow points are given equal weight (1.0); the gaussian blur
    produces a smooth halo at zone boundaries.  The dark-indigo palette
    is visually distinct from the coverage layer so both can be overlaid
    simultaneously without confusion.

    A slightly larger sigma (2.5 vs 1.8 for coverage) softens the
    boundaries further, making the dead-zone extents easier to read.
    """
    if len(lats) == 0:
        return None

    # Uniform value — every shadow point is equally "blocked"
    values = np.ones(len(lats), dtype=np.float32)

    return render_raster_png(
        lats, lons, values,
        grid_deg=grid_deg,
        sigma_cells=sigma_cells,
        colour_stops=_SHADOW_COLOUR_STOPS,
        vmin=0.0,
        vmax=1.0,
        alpha_min=25,
        alpha_max=165,
    )
