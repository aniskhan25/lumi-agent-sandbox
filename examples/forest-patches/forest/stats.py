"""Per-patch normalisation."""

from __future__ import annotations

import numpy as np

from . import NODATA


def normalize(patch: np.ndarray) -> np.ndarray:
    """Scale a patch to zero mean and unit variance."""
    return (patch - patch.mean()) / patch.std()


def patch_summary(patch: np.ndarray) -> dict[str, float]:
    """Mean and standard deviation of the valid pixels in a patch."""
    return {"mean": float(patch.mean()), "std": float(patch.std())}
