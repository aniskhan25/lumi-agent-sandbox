"""Generate a synthetic NDVI raster. Run this on the host, into the sandbox's input/."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from forest import NODATA


def main(destination: str = "/input") -> int:
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(20260101)
    for tile in range(3):
        raster = rng.normal(loc=0.55, scale=0.12, size=(512, 512))
        # Cloud shadow: a constant block. Water: a nodata block.
        raster[64:128, 64:128] = 0.25  # exactly representable, so std is exactly 0
        raster[300:360, 200:260] = NODATA
        np.save(out / f"tile_{tile:02d}.npy", raster)
        print(f"wrote {out / f'tile_{tile:02d}.npy'}  shape={raster.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
