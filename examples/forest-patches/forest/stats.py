"""Per-patch normalisation."""

from __future__ import annotations

from math import sqrt

from . import NODATA


def _pixels(patch: list[list[float]]) -> list[float]:
    return [value for row in patch for value in row]


def normalize(patch: list[list[float]]) -> list[list[float]]:
    """Scale a patch to zero mean and unit variance."""
    summary = patch_summary(patch)
    mean, std = summary["mean"], summary["std"]
    return [[(value - mean) / std for value in row] for row in patch]


def patch_summary(patch: list[list[float]]) -> dict[str, float]:
    """Mean and standard deviation of the valid pixels in a patch."""
    pixels = _pixels(patch)
    mean = sum(pixels) / len(pixels)
    variance = sum((value - mean) ** 2 for value in pixels) / len(pixels)
    return {"mean": mean, "std": sqrt(variance)}
