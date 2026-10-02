"""Tile a raster into patches for model input."""

from __future__ import annotations

import numpy as np


def patch_grid(height: int, width: int, size: int, stride: int) -> list[tuple[int, int]]:
    """Top-left corners of every patch that fits fully inside the raster."""
    rows = range(0, height - size, stride)
    cols = range(0, width - size, stride)
    return [(r, c) for r in rows for c in cols]


def extract_patches(raster: np.ndarray, size: int, stride: int) -> np.ndarray:
    """Stack of (n, size, size) patches taken from `raster`."""
    if raster.ndim != 2:
        raise ValueError(f"expected a 2-D raster, got shape {raster.shape}")
    corners = patch_grid(raster.shape[0], raster.shape[1], size, stride)
    return np.stack([raster[r : r + size, c : c + size] for r, c in corners])
