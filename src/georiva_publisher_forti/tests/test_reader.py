"""Reading one bbox out of many COGs, through core's read module.

Real GeoTIFFs on a real (filesystem) assets bucket, read by core's
``values.session`` — nothing between the raster and the cube is stubbed. Two
things are held here that nothing downstream could notice going wrong:

- **Units are converted on read.** A COG holds its Variable's source unit
  (ADR 0046 §1); the publisher asks for the unit its slot publishes, and gets it.
- **Point ordinal *i* is the cell at grid point *i*.** Forti addresses every value
  by its position in the point list, so the list and the cube must come from the
  same window, flattened the same way. Each test raster here holds, in every
  cell, that cell's own position in the whole raster — so a value read back says
  exactly which cell it came from, and the grid can be checked against it.
"""

from datetime import UTC, datetime

import numpy as np
import rasterio
from django.test import TestCase
from rasterio.transform import from_origin

from georiva.core import values
from georiva.core.models import Asset, Item, Unit, Variable
from georiva_publisher_forti.reader import WindowReader

from .assets_on_disk import AssetsOnDiskMixin
from .factories import make_collection

TIME = datetime(2026, 9, 2, 12, tzinfo=UTC)

#: Global 0.25°, origin at the top-left corner: the geometry of the IFS COGs.
GLOBAL = from_origin(-180.0, 90.0, 0.25, 0.25)
GLOBAL_WIDTH, GLOBAL_HEIGHT = 1440, 721

KENYA = (33.0, -5.5, 42.9, 6.3)


class ReaderTestCase(AssetsOnDiskMixin, TestCase):
    def setUp(self):
        self.root = self.isolate_assets()
        self.collection = make_collection()
        self.item = Item.objects.create(collection=self.collection, time=TIME, reference_time=TIME)

    def variable(self, slug, source_symbol, served_symbol):
        source, _ = Unit.objects.get_or_create(symbol=source_symbol, defaults={"name": source_symbol})
        served, _ = Unit.objects.get_or_create(symbol=served_symbol, defaults={"name": served_symbol})
        return Variable.objects.create(
            collection=self.collection, slug=slug, name=slug, unit=served, source_unit=source
        )

    def cog(self, variable, name, data, transform, crs="EPSG:4326"):
        """A tiled float32 GeoTIFF on the assets bucket, and the Asset naming it."""
        href = f"{variable.slug}/{name}.tif"
        path = self.root / href
        path.parent.mkdir(parents=True, exist_ok=True)
        height, width = data.shape
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        ) as destination:
            destination.write(data.astype("float32"), 1)
        item = (
            self.item
            if name == "00"
            else Item.objects.get_or_create(
                collection=self.collection, time=TIME.replace(hour=int(name)), reference_time=TIME
            )[0]
        )
        return Asset.objects.create(item=item, variable=variable, href=href, format=Asset.Format.COG)


def positions(height, width):
    """Every cell holding its own row-major position in the whole raster."""
    return np.arange(height * width, dtype="float64").reshape(height, width)


class UnitTests(ReaderTestCase):
    def test_a_kelvin_cog_is_read_in_the_unit_the_slot_publishes(self):
        variable = self.variable("t2m-k", "K", "K")
        asset = self.cog(variable, "00", np.full((40, 40), 300.0), from_origin(30.0, 10.0, 0.25, 0.25))

        with values.session() as session:
            reader = WindowReader(session, (32.0, 4.0, 33.25, 5.25))
            reader.resolve(asset)
            cube = reader.read_series([asset], in_unit="degC")

        self.assertEqual(cube.shape, (1, 25))
        np.testing.assert_allclose(cube, 26.85, atol=1e-4)

    def test_a_unit_the_source_cannot_reach_is_refused_before_anything_is_read(self):
        variable = self.variable("msl-pa", "Pa", "Pa")
        asset = self.cog(variable, "00", np.full((40, 40), 101300.0), from_origin(30.0, 10.0, 0.25, 0.25))

        with values.session() as session, self.assertRaises(values.ConversionRefused):
            reader = WindowReader(session, (32.0, 4.0, 33.25, 5.25))
            reader.resolve(asset)
            reader.read_series([asset], in_unit="degC")


class OrdinalTests(ReaderTestCase):
    def test_kenya_is_the_window_the_spike_measured(self):
        """48 rows by 40 columns, starting at pixel (334, 852) of the global grid —
        what the spike measured, and what every pinned Kenya grid was built from."""
        variable = self.variable("2t-global", "degC", "degC")
        asset = self.cog(variable, "00", positions(GLOBAL_HEIGHT, GLOBAL_WIDTH), GLOBAL)

        with values.session() as session:
            grid = WindowReader(session, KENYA).resolve(asset)

        self.assertEqual((grid.rows, grid.cols), (48, 40))
        # The centre of pixel (334, 852): 90 − 334.5·0.25 and −180 + 852.5·0.25.
        self.assertEqual((float(grid.latitude[0]), float(grid.longitude[0])), (6.375, 33.125))

    def test_every_value_lands_on_the_point_it_was_read_from(self):
        variable = self.variable("2t-global", "degC", "degC")
        asset = self.cog(variable, "00", positions(GLOBAL_HEIGHT, GLOBAL_WIDTH), GLOBAL)

        with values.session() as session:
            reader = WindowReader(session, KENYA)
            grid = reader.resolve(asset)
            (cube,) = reader.read_series([asset], in_unit="degC")

        # float32 holds these positions exactly (all below 2**24).
        rows, cols = np.divmod(cube.astype(np.int64), GLOBAL_WIDTH)
        np.testing.assert_array_equal(grid.latitude, (90.0 - (rows + 0.5) * 0.25).astype("<f4"))
        np.testing.assert_array_equal(grid.longitude, (-180.0 + (cols + 0.5) * 0.25).astype("<f4"))

    def test_a_cog_with_another_geometry_is_refused(self):
        """A shifted grid would file every value under the wrong coordinates."""
        variable = self.variable("2t-global", "degC", "degC")
        first = self.cog(variable, "00", positions(40, 40), from_origin(30.0, 10.0, 0.25, 0.25))
        shifted = self.cog(variable, "03", positions(40, 40), from_origin(30.1, 10.0, 0.25, 0.25))

        with values.session() as session, self.assertRaises(ValueError) as ctx:
            reader = WindowReader(session, (32.0, 4.0, 33.25, 5.25))
            reader.resolve(first)
            reader.read_series([first, shifted], in_unit="degC")

        self.assertIn("different raster geometry", str(ctx.exception))

    def test_a_box_reaching_past_the_raster_is_clipped_not_refused(self):
        """An area at the edge of a regional model is ordinary configuration."""
        variable = self.variable("2t-edge", "degC", "degC")
        asset = self.cog(variable, "00", positions(40, 40), from_origin(30.0, 10.0, 0.25, 0.25))

        with values.session() as session:
            grid = WindowReader(session, (29.0, 9.0, 30.5, 11.0)).resolve(asset)

        self.assertEqual((grid.rows, grid.cols), (4, 2))
        self.assertEqual((float(grid.latitude[0]), float(grid.longitude[0])), (9.875, 30.125))

    def test_a_backwards_box_is_refused(self):
        variable = self.variable("2t-edge", "degC", "degC")
        asset = self.cog(variable, "00", positions(40, 40), from_origin(30.0, 10.0, 0.25, 0.25))

        with values.session() as session, self.assertRaises(ValueError):
            WindowReader(session, (33.25, 4.0, 32.0, 5.25)).resolve(asset)

    def test_a_box_that_misses_the_raster_is_refused(self):
        variable = self.variable("2t-global", "degC", "degC")
        asset = self.cog(variable, "00", positions(40, 40), from_origin(0.0, 10.0, 0.25, 0.25))

        with values.session() as session, self.assertRaises(ValueError) as ctx:
            WindowReader(session, (100.0, 0.0, 110.0, 5.0)).resolve(asset)

        self.assertIn("does not overlap", str(ctx.exception))

    def test_a_raster_not_in_longitude_and_latitude_is_refused(self):
        """Forti's points are latitude and longitude; a projected grid's
        coordinates are metres."""
        variable = self.variable("2t-merc", "degC", "degC")
        asset = self.cog(
            variable, "00", positions(40, 40), from_origin(3_000_000, 1_000_000, 25_000, 25_000), "EPSG:3857"
        )

        with values.session() as session, self.assertRaises(ValueError) as ctx:
            WindowReader(session, (28.0, 1.0, 33.0, 5.0)).resolve(asset)

        self.assertIn("EPSG:4326", str(ctx.exception))
