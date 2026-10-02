import unittest
from math import isnan

from forest import NODATA
from forest.patches import extract_patches, patch_grid
from forest.stats import normalize, patch_summary


def constant(size, value):
    return [[value] * size for _ in range(size)]


class PatchGridTests(unittest.TestCase):
    def test_grid_covers_the_whole_raster(self) -> None:
        # A 100x100 raster tiled with 50x50 patches at stride 50 is exactly 2x2.
        grid = patch_grid(100, 100, size=50, stride=50)
        self.assertEqual(len(grid), 4)
        # And the bottom-right patch must be one of them.
        self.assertIn((50, 50), grid)

    def test_overlapping_grid_keeps_the_last_window(self) -> None:
        # 0, 25 and 50 all fit a 50-wide window in a 100-wide raster.
        self.assertEqual(patch_grid(100, 50, size=50, stride=25), [(0, 0), (25, 0), (50, 0)])

    def test_extract_matches_the_grid(self) -> None:
        raster = [[r * 100 + c for c in range(100)] for r in range(100)]
        patches = extract_patches(raster, size=50, stride=50)

        self.assertEqual(len(patches), 4)
        self.assertEqual(len(patches[0]), 50)
        self.assertEqual(len(patches[0][0]), 50)
        # The last patch is the bottom-right corner of the raster.
        self.assertEqual(patches[-1][0][0], raster[50][50])
        self.assertEqual(patches[-1][-1][-1], raster[99][99])


class NormalisationTests(unittest.TestCase):
    def test_nodata_pixels_do_not_skew_statistics(self) -> None:
        patch = constant(10, 5.0)
        patch[0][0] = NODATA

        summary = patch_summary(patch)

        # 99 pixels are 5.0 and one is a nodata sentinel. The mean of the valid
        # data is 5.0; including the sentinel drags it to about -95.
        self.assertAlmostEqual(summary["mean"], 5.0, places=6)
        self.assertAlmostEqual(summary["std"], 0.0, places=6)

    def test_normalize_ignores_nodata(self) -> None:
        patch = [[float(r * 10 + c) for c in range(10)] for r in range(10)]
        patch[0][0] = NODATA

        scaled = normalize(patch)

        valid = [
            scaled[r][c]
            for r in range(10)
            for c in range(10)
            if patch[r][c] != NODATA
        ]
        mean = sum(valid) / len(valid)
        self.assertAlmostEqual(mean, 0.0, places=6)

    def test_constant_patch_does_not_blow_up(self) -> None:
        # A cloud-masked or water patch is constant; dividing by its zero
        # standard deviation must not crash or poison the batch.
        scaled = normalize(constant(8, 3.0))
        flat = [value for row in scaled for value in row]
        self.assertFalse(any(isnan(value) for value in flat), "constant patch produced NaNs")


if __name__ == "__main__":
    unittest.main()
