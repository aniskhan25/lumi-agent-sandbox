"""Generate synthetic NDVI rasters. Run on the host, into the sandbox's input/.

Plain JSON and the standard library only: the agent container has no numpy, and
a sample task should exercise the sandbox rather than a package install.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from forest import NODATA

SIZE = 256


def main(destination: str = "/input") -> int:
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)

    rng = random.Random(20260101)
    for tile in range(3):
        raster = [[round(rng.gauss(0.55, 0.12), 6) for _ in range(SIZE)] for _ in range(SIZE)]
        # Cloud shadow, aligned to one whole patch so it is exactly constant.
        for r in range(64, 128):
            for c in range(64, 128):
                raster[r][c] = 0.25
        # Water, straddling patches as it would in a real scene.
        for r in range(150, 190):
            for c in range(100, 140):
                raster[r][c] = NODATA

        path = out / f"tile_{tile:02d}.json"
        path.write_text(json.dumps({"shape": [SIZE, SIZE], "data": raster}), encoding="utf-8")
        print(f"wrote {path}  shape={SIZE}x{SIZE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
