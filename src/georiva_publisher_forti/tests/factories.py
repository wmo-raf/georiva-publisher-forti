"""A collection, a run, and COGs on disk that rasterio can actually open.

The publisher's whole job is turning rasters into bytes, so a test that fakes the
raster tests the half that was never in doubt. These write real GeoTIFFs into a
temporary directory and point ``reader.asset_url`` at them, which is the one seam
between this plugin and object storage.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
import rasterio
from rasterio.transform import from_origin

from georiva.core.models import Asset, Catalog, Collection, Item, Unit, Variable
from georiva.ingestion.models import RunIngestion
from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG, make_organisation
from georiva_publisher_forti.models import FortiPublication

REFERENCE_TIME = datetime(2026, 9, 2, 12, tzinfo=UTC)

#: A small raster rather than the real 1440x721 — the geometry that matters is
#: the transform, and 40x40 keeps the test fast.
WIDTH = HEIGHT = 40
TRANSFORM = from_origin(30.0, 10.0, 0.25, 0.25)

#: Inside the raster, five by five points.
BBOX = (32.0, 4.0, 33.25, 5.25)

UNITS = {
    "2t": ("Degree Celsius", "degC"),
    "2d": ("Degree Celsius", "degC"),
    "msl": ("Hectopascal", "hPa"),
    "wind_speed_10m": ("Metre per second", "m/s"),
    "wind_dir_10m": ("Degree", "deg"),
    "10fg": ("Metre per second", "m/s"),
    "tcc": ("Percent", "%"),
    "tp": ("Millimetre", "mm"),
}

#: One plausible constant per variable. ``tp`` is handled separately — it
#: accumulates.
CONSTANTS = {
    "2t": 24.0,
    "2d": 18.0,
    "msl": 1013.0,
    "wind_speed_10m": 4.5,
    "wind_dir_10m": 120.0,
    "10fg": 9.0,
    "tcc": 45.0,
}


def make_collection(slug="ifs-surface", visibility=None, org_slug=DEFAULT_TEST_ORG_SLUG):
    """One organisation's collection, with the variables the publisher reads.

    ``org_slug`` is how a test asks for a *second* organisation. It matters now
    that every area shares one prefix: the tenancy questions — two organisations
    publishing the same area name, a prune that must not reach past its own key —
    cannot be asked with only one organisation in the database.
    """
    organisation = make_organisation(org_slug)
    catalog = Catalog.objects.create(
        organisation=organisation,
        name="ECMWF IFS",
        # Unique per collection: two publications of one organisation is the
        # normal case (a coarse area and a fine one), and catalog slugs are
        # unique within an organisation.
        slug=f"ecmwf-{slug}",
        file_format="grib2",
    )
    collection = Collection.objects.create(catalog=catalog, name="Surface", slug=slug)
    if visibility is not None:
        collection.visibility = visibility
        collection.save(update_fields=["visibility"])

    for variable_slug, (unit_name, symbol) in UNITS.items():
        unit, _ = Unit.objects.get_or_create(name=unit_name, defaults={"symbol": symbol})
        Variable.objects.create(
            collection=collection,
            slug=variable_slug,
            name=variable_slug,
            unit=unit,
            value_min=-100,
            value_max=2000,
        )
    return collection


def make_run(collection, reference_time=REFERENCE_TIME, closed=True):
    run = RunIngestion.objects.create(collection=collection, reference_time=reference_time)
    if closed:
        run.close(RunIngestion.Closer.DECLARED_SET)
        run.refresh_from_db()
    return run


def write_cogs(collection, directory, step_hours=3, steps=9, reference_time=REFERENCE_TIME, skip=()):
    """One GeoTIFF per variable per step, and the Items and Assets naming them.

    ``skip`` names variables to leave out of the *last* step, which is how a real
    run looks while its stragglers are still ingesting.
    """
    variables = {variable.slug: variable for variable in collection.variables.all()}
    hrefs = {}

    for step in range(steps):
        valid_time = reference_time + timedelta(hours=step * step_hours)
        item, _ = Item.objects.get_or_create(
            collection=collection,
            time=valid_time,
            reference_time=reference_time,
        )

        for slug, variable in variables.items():
            if slug in skip and step == steps - 1:
                continue

            value = step * 0.5 if slug == "tp" else CONSTANTS[slug]
            href = f"{slug}/{step:02d}.tif"
            path = directory / href
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_tif(path, value)

            Asset.objects.get_or_create(
                item=item,
                variable=variable,
                href=href,
                format=Asset.Format.COG,
            )
            hrefs[href] = path

    return hrefs


def make_publication(collection, area="kenya", bbox=BBOX, **kwargs):
    west, south, east, north = bbox
    return FortiPublication.objects.create(
        collection=collection,
        area=area,
        west=west,
        south=south,
        east=east,
        north=north,
        **kwargs,
    )


def _write_tif(path, value):
    data = np.full((HEIGHT, WIDTH), value, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=HEIGHT,
        width=WIDTH,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=TRANSFORM,
    ) as destination:
        destination.write(data, 1)
