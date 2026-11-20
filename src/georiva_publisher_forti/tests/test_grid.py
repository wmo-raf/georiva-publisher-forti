"""The point list, and why it must be the same one every run.

``index.Add`` caches the s2 spatial index under the MD5 of the coordinate bytes
(`georeader.Checksum`), and ``Dataset.Close`` never calls ``index.Free``
(`dataset.go:123`). A grid that is identical run to run is therefore built once
and reused, and the leak stays theoretical. A grid that moves leaks an index per
run — and, worse, invalidates every ordinal already stored against it.
"""

from django.test import SimpleTestCase

from georiva_publisher_forti.grid import from_raster_window

#: Global 0.25 degree, north-up, origin at the top-left corner.
GLOBAL_QUARTER_DEGREE = (0.25, 0.0, -180.0, 0.0, -0.25, 90.0)

#: Kenya's window on it, ``(row offset, col offset, rows, cols)``: what the
#: spike measured, and what ``test_reader`` holds core's window read to.
KENYA = (334, 852, 48, 40)


class GridTests(SimpleTestCase):
    def _kenya(self):
        row_off, col_off, rows, cols = KENYA
        return from_raster_window(GLOBAL_QUARTER_DEGREE, rows, cols, row_off, col_off)

    def test_the_same_configuration_produces_the_same_identifier(self):
        self.assertEqual(self._kenya().identifier, self._kenya().identifier)

    def test_a_different_box_produces_a_different_identifier(self):
        row_off, col_off, rows, cols = KENYA
        other = from_raster_window(GLOBAL_QUARTER_DEGREE, rows - 1, cols, row_off + 1, col_off)

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
