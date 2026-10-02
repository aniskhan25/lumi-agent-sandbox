"""Tile every input raster and summarise the patches.

Reads /input, writes /output. Inside the sandbox those are the read-only input
mount and the results mount, so this runs unchanged as a batch job.
"""

from __future__ import annotations

import json
import sys
from math import isnan
from pathlib import Path

from forest.patches import extract_patches
from forest.stats import normalize, patch_summary

PATCH = 64
STRIDE = 64


def main(source: str = "/input", destination: str = "/output") -> int:
    tiles = sorted(Path(source).glob("*.json"))
    if not tiles:
        print(f"no .json rasters in {source}", file=sys.stderr)
        return 1

    report = []
    for tile in tiles:
        raster = json.loads(tile.read_text(encoding="utf-8"))["data"]
        patches = extract_patches(raster, size=PATCH, stride=STRIDE)

        bad = 0
        for patch in patches:
            try:
                scaled = normalize(patch)
            except ZeroDivisionError:
                bad += 1
                continue
            if any(isnan(v) for row in scaled for v in row):
                bad += 1

        expected = (len(raster) // STRIDE) * (len(raster[0]) // STRIDE)
        summary = {
            "tile": tile.name,
            "raster": [len(raster), len(raster[0])],
            "patches": len(patches),
            "patches_expected": expected,
            "covers_raster": len(patches) == expected,
            "bad_patches": bad,
            "first_patch": patch_summary(patches[0]),
        }
        report.append(summary)
        print(f"{tile.name}: {len(patches)}/{expected} patches, {bad} failed to scale")

    out = Path(destination)
    out.mkdir(parents=True, exist_ok=True)
    (out / "patch_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    healthy = all(s["covers_raster"] and s["bad_patches"] == 0 for s in report)
    print(f"\nwrote {out / 'patch_report.json'}")
    print("ALL TILES HEALTHY" if healthy else "PROBLEMS FOUND - see patch_report.json")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
