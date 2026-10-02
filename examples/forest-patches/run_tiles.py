"""Tile every input raster and summarise the patches.

Reads /input, writes /output. Inside the sandbox those are the read-only input
mount and the results mount, so this runs unchanged as a batch job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from forest.patches import extract_patches
from forest.stats import normalize, patch_summary

PATCH = 64
STRIDE = 64


def main(source: str = "/input", destination: str = "/output") -> int:
    tiles = sorted(Path(source).glob("*.npy"))
    if not tiles:
        print(f"no .npy rasters in {source}", file=sys.stderr)
        return 1

    report = []
    for tile in tiles:
        raster = np.load(tile)
        patches = extract_patches(raster, size=PATCH, stride=STRIDE)
        scaled = np.stack([normalize(p) for p in patches])

        expected = (raster.shape[0] // STRIDE) * (raster.shape[1] // STRIDE)
        summary = {
            "tile": tile.name,
            "raster": list(raster.shape),
            "patches": int(patches.shape[0]),
            "patches_expected": int(expected),
            "covers_raster": int(patches.shape[0]) == int(expected),
            "nan_patches": int(np.isnan(scaled).any(axis=(1, 2)).sum()),
            "first_patch": patch_summary(patches[0]),
        }
        report.append(summary)
        print(
            f"{tile.name}: {summary['patches']}/{expected} patches, "
            f"{summary['nan_patches']} with NaNs after scaling"
        )

    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    (out / "patch_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    healthy = all(s["covers_raster"] and s["nan_patches"] == 0 for s in report)
    print(f"\nwrote {out / 'patch_report.json'}")
    print("ALL TILES HEALTHY" if healthy else "PROBLEMS FOUND - see patch_report.json")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
