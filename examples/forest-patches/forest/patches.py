"""Tile a raster into patches for model input."""

from __future__ import annotations


def patch_grid(height: int, width: int, size: int, stride: int) -> list[tuple[int, int]]:
    """Top-left corners of every patch that fits fully inside the raster."""
    rows = range(0, height - size, stride)
    cols = range(0, width - size, stride)
    return [(r, c) for r in rows for c in cols]


def extract_patches(raster: list[list[float]], size: int, stride: int) -> list[list[list[float]]]:
    """Every patch taken from `raster`, as a list of size x size grids."""
    if not raster or not raster[0]:
        raise ValueError("raster is empty")
    height, width = len(raster), len(raster[0])
    return [
        [row[c : c + size] for row in raster[r : r + size]]
        for r, c in patch_grid(height, width, size, stride)
    ]
