import unittest

import numpy as np

from forest import NODATA
from forest.patches import extract_patches, patch_grid
from forest.stats import normalize, patch_summary


class PatchGridTests(unittest.TestCase):
    def test_grid_covers_the_whole_raster(self) -> None:
        # A 100x100 raster tiled with 50x50 patches at stride 50 is exactly 2x2.
        self.assertEqual(len(patch_grid(100, 100, size=50, stride=50)), 4)

        # And the bottom-right patch must be one of them.
        self.assertIn((50, 50), patch_grid(100, 100, size=50, stride=50))

    def test_overlapping_grid_keeps_the_last_window(self) -> None:
        # 0, 25, 50 all fit a 50-wide window in a 100-wide raster.
        self.assertEqual(patch_grid(100, 50, size=50, stride=25), [(0, 0), (25, 0), (50, 0)])

    def test_extract_matches_the_grid(self) -> None:
        raster = np.arange(100 * 100, dtype=float).reshape(100, 100)
        patches = extract_patches(raster, size=50, stride=50)
        self.assertEqual(patches.shape, (4, 50, 50))
        # Last patch is the bottom-right corner of the raster.
        np.testing.assert_array_equal(patches[-1], raster[50:100, 50:100])


class NormalisationTests(unittest.TestCase):
    def test_nodata_pixels_do_not_skew_statistics(self) -> None:
        patch = np.full((10, 10), 5.0)
        patch[0, 0] = NODATA

        summary = patch_summary(patch)

        # 99 pixels are 5.0 and one is a nodata sentinel. The mean of the valid
        # data is 5.0; including the sentinel drags it to about -94.
        self.assertAlmostEqual(summary["mean"], 5.0, places=6)
        self.assertAlmostEqual(summary["std"], 0.0, places=6)

    def test_normalize_ignores_nodata(self) -> None:
        patch = np.arange(100, dtype=float).reshape(10, 10)
        patch[0, 0] = NODATA

        scaled = normalize(patch)

        valid = scaled[patch != NODATA]
        self.assertAlmostEqual(float(valid.mean()), 0.0, places=6)
        self.assertAlmostEqual(float(valid.std()), 1.0, places=6)

    def test_constant_patch_does_not_produce_nan(self) -> None:
        # A cloud-masked or water patch is constant; dividing by its zero
        # standard deviation must not poison the batch with NaNs.
        scaled = normalize(np.full((8, 8), 3.0))
        self.assertFalse(np.isnan(scaled).any(), "constant patch produced NaNs")


if __name__ == "__main__":
    unittest.main()
