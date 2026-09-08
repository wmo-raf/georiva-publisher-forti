"""Reading one bbox out of many COGs, cheaply.

The whole reason this plugin exists is that a point query against the COGs is
expensive: one COG per variable per timestep, global 0.25°, 1440x721, 256x256
tiles at about 1.76 MB each. Answering a single coordinate costs ~950 range reads
and ~93 MB across 14 API calls.

The publish side gets to do the opposite of what a point query does. Kenya at
0.25° falls inside a *single* 256x256 tile (pixel x 855-968, y 339-379), so
reading the whole area out of one COG is one windowed read of about 96 KB — not
the 1.76 MB the object weighs. Roughly 750 of those, once per run.

That economy only exists if the read is windowed. ``storage.assets.read_bytes``,
which the zonal-stats path uses, pulls the entire object; over a run that is 1.3
GB instead of 72 MB. So this module goes through GDAL's HTTP reader against the
assets bucket, which is public-read and therefore needs no credential — the same
route the tile server takes.
"""

import logging

import numpy as np
import rasterio
from django.conf import settings
from rasterio.windows import Window

logger = logging.getLogger(__name__)

#: Opening a COG otherwise makes GDAL list the directory it sits in, hunting for
#: sidecars a COG never has — a request per open, and a cached listing that hides
#: siblings written afterwards. The same pin the tile server depends on (#400).
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "VSI_CACHE": "TRUE",
}


def asset_url(href: str) -> str:
    """The URL GDAL reads a published COG through.

    The assets bucket is public-read (`setup_minio`), so this is a plain HTTP
    GET and no credential travels with it. The internal endpoint is used because
    the worker is inside the compose network.
    """
    endpoint = settings.AWS_S3_ENDPOINT_URL.rstrip("/")
    bucket = settings.GEORIVA_ASSETS_BUCKET
    return f"{endpoint}/{bucket}/{href}"


class WindowReader:
    """Reads the same pixel window out of a series of COGs.

    The window is resolved once from the first COG's geometry, and every later
    read is checked against it: a COG whose transform or shape differs cannot
    contribute to the same point list, and quietly reading it anyway would file
    values under coordinates they do not belong to.
    """

    def __init__(self, bbox):
        self.bbox = bbox
        self.transform = None
        self.width = None
        self.height = None
        self.window = None

    def resolve(self, href: str):
        """Open one COG to fix the geometry; returns ``(row_off, col_off, rows, cols)``."""
        from .grid import window_for_bbox

        with rasterio.Env(**GDAL_ENV), rasterio.open(asset_url(href)) as source:
            self.transform = tuple(source.transform)[:6]
            self.width, self.height = source.width, source.height

        row_off, col_off, rows, cols = window_for_bbox(self.transform, self.width, self.height, self.bbox)
        self.window = Window(col_off=col_off, row_off=row_off, width=cols, height=rows)
        return row_off, col_off, rows, cols

    def read(self, href: str) -> np.ndarray:
        """The window from one COG, as a ``(rows, cols)`` float32 array."""
        if self.window is None:
            self.resolve(href)

        with rasterio.Env(**GDAL_ENV), rasterio.open(asset_url(href)) as source:
            if tuple(source.transform)[:6] != self.transform or (source.width, source.height) != (
                self.width,
                self.height,
            ):
                raise ValueError(
                    f"{href} has a different raster geometry from the rest of the run. "
                    f"Every value in a Forti area is addressed by point ordinal, so a "
                    f"shifted grid files values under the wrong coordinates."
                )
            return source.read(1, window=self.window, masked=False).astype(np.float32)

    def read_series(self, hrefs) -> np.ndarray:
        """One variable's whole run over the window, as ``(time, point)``.

        Flattened row-major, matching ``grid.from_raster_window``'s ordering —
        the point ordinal is the only thing tying a value to a place.
        """
        frames = [self.read(href).ravel(order="C") for href in hrefs]
        return np.stack(frames)
