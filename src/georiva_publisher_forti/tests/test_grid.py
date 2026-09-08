"""The point list, and why it must be the same one every run.

``index.Add`` caches the s2 spatial index under the MD5 of the coordinate bytes
(`georeader.Checksum`), and ``Dataset.Close`` never calls ``index.Free``
(`dataset.go:123`). A grid that is identical run to run is therefore built once
and reused, and the leak stays theoretical. A grid that moves leaks an index per
run — and, worse, invalidates every ordinal already stored against it.
"""

from django.test import SimpleTestCase

from georiva_publisher_forti.grid import from_raster_window, window_for_bbox

#: Global 0.25 degree, north-up, origin at the top-left corner.
GLOBAL_QUARTER_DEGREE = (0.25, 0.0, -180.0, 0.0, -0.25, 90.0)
WIDTH, HEIGHT = 1440, 721

KENYA = (33.0, -5.5, 42.9, 6.3)


class WindowTests(SimpleTestCase):
    def test_kenya_resolves_to_the_window_the_spike_measured(self):
        row_off, col_off, rows, cols = window_for_bbox(GLOBAL_QUARTER_DEGREE, WIDTH, HEIGHT, KENYA)

        self.assertEqual((rows, cols), (48, 40))
        self.assertEqual(rows * cols, 1920)
        self.assertEqual((row_off, col_off), (334, 852))

    def test_a_box_reaching_past_the_raster_is_clipped_not_refused(self):
        """An area at the edge of a regional model is ordinary configuration."""
        row_off, col_off, rows, cols = window_for_bbox(
            GLOBAL_QUARTER_DEGREE, WIDTH, HEIGHT, (-181.0, 80.0, -170.0, 95.0)
        )

        self.assertEqual(col_off, 0)
        self.assertEqual(row_off, 0)
        self.assertGreater(rows * cols, 0)

    def test_a_box_that_misses_the_raster_entirely_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            window_for_bbox((0.25, 0.0, 0.0, 0.0, -0.25, 10.0), 40, 40, (100.0, 0.0, 110.0, 5.0))

        self.assertIn("does not overlap", str(ctx.exception))

    def test_a_backwards_box_is_refused(self):
        with self.assertRaises(ValueError):
            window_for_bbox(GLOBAL_QUARTER_DEGREE, WIDTH, HEIGHT, (42.9, -5.5, 33.0, 6.3))


class GridTests(SimpleTestCase):
    def _kenya(self):
        row_off, col_off, rows, cols = window_for_bbox(GLOBAL_QUARTER_DEGREE, WIDTH, HEIGHT, KENYA)
        return from_raster_window(GLOBAL_QUARTER_DEGREE, rows, cols, row_off, col_off)

    def test_the_same_configuration_produces_the_same_identifier(self):
        self.assertEqual(self._kenya().identifier, self._kenya().identifier)

    def test_a_different_box_produces_a_different_identifier(self):
        row_off, col_off, rows, cols = window_for_bbox(GLOBAL_QUARTER_DEGREE, WIDTH, HEIGHT, (33.0, -5.5, 42.9, 6.0))
        other = from_raster_window(GLOBAL_QUARTER_DEGREE, rows, cols, row_off, col_off)

        self.assertNotEqual(self._kenya().identifier, other.identifier)

    def test_coordinates_are_pixel_centres_not_corners(self):
        """A quarter-degree cell's corner is 14 km from its middle, and a point
        forecast is for a place."""
        grid = self._kenya()

        self.assertEqual(float(grid.longitude[0]) % 0.25, 0.125)

    def test_points_run_row_major_so_ordinals_match_a_c_order_ravel(self):
        grid = from_raster_window(GLOBAL_QUARTER_DEGREE, 2, 3, 0, 0)

        self.assertEqual(grid.latitude.tolist(), [90.0 - 0.125] * 3 + [90.0 - 0.375] * 3)
        self.assertEqual(
            [round(value, 3) for value in grid.longitude.tolist()],
            [-179.875, -179.625, -179.375] * 2,
        )

    def test_coordinates_are_little_endian_float32(self):
        """What Forti reads. Anything else decodes as noise."""
        grid = self._kenya()

        self.assertEqual(grid.latitude.dtype.str, "<f4")
        self.assertEqual(grid.longitude.dtype.str, "<f4")
