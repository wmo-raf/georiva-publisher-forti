"""The point list a publication publishes, and why it must not move.

Forti's grid is an **unstructured point list**, not a raster: ``latitude`` and
``longitude`` are flat float32 arrays and ``data`` is indexed by point ordinal.
Two consequences follow, and both are load-bearing.

**We choose which points exist.** A masked cell simply does not appear — which
matters because the format has no missing-value convention anywhere. A NaN
encodes as ``-32768`` and decodes as a perfectly plausible ``-3276.8``, so
"publish only points that have data" is the only safe answer.

**The grid must be identical run to run.** ``index.Add`` caches the s2 spatial
index keyed on the MD5 of the lat and lon bytes (`georeader.Checksum`), so a grid
that does not move is built once and reused. It is also the condition under which
the known leak in `dataset.go:123` — ``index.Free`` exists and is correct, but
``Dataset.Close`` never calls it — stays harmless: a grid that changes shape every
run leaks an index every run.

So the grid is a function of the publication's own bbox and the collection's
raster geometry — the window core's read module returns for that bbox
(:mod:`.reader`) — and the identifier is pinned on the publication the first time it
is built. A later build that produces a different identifier is refused rather
than published: it means the source geometry moved, which is a thing to find out
about rather than to leak over.
"""

import hashlib
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Grid:
    """The points of one area, in the order ``data`` indexes them.

    ``latitude`` and ``longitude`` are float32 and parallel: point *i* is at
    ``(latitude[i], longitude[i])``. The order is the row-major flattening of the
    raster window they came from, and every values array must be flattened the
    same way — the ordinal is the only thing tying a value to a place.
    """

    latitude: np.ndarray
    longitude: np.ndarray
    rows: int
    cols: int

    def __post_init__(self):
        if self.latitude.dtype != np.dtype("<f4") or self.longitude.dtype != np.dtype("<f4"):
            raise ValueError("Grid coordinates must be little-endian float32 — that is what Forti reads")
        if self.latitude.size != self.longitude.size:
            raise ValueError("latitude and longitude must have one entry each per point")
        if self.latitude.size != self.rows * self.cols:
            raise ValueError(f"{self.latitude.size} points do not fill a {self.rows}x{self.cols} window")

    @property
    def point_count(self) -> int:
        return int(self.latitude.size)

    @property
    def identifier(self) -> str:
        """MD5 of the coordinate bytes — the directory name, and the key
        ``rawdataforecaster`` caches its s2 index under."""
        return hashlib.md5(self.latitude.tobytes() + self.longitude.tobytes()).hexdigest()


def from_raster_window(transform, rows: int, cols: int, row_offset: int, col_offset: int) -> Grid:
    """Build the point list for a window of a raster, at pixel centres.

    ``transform`` is an affine transform in rasterio's order
    ``(a, b, c, d, e, f)`` — ``c`` and ``f`` are the top-left corner of the
    raster, ``a`` and ``e`` the pixel size. Coordinates are taken at pixel
    *centres*, because a point forecast is for a place and not for a corner: a
    quarter-degree cell's corner is 14 km from its middle.
    """
    pixel_width, _, origin_x, _, pixel_height, origin_y = transform[:6]

    columns = col_offset + np.arange(cols) + 0.5
    lines = row_offset + np.arange(rows) + 0.5

    longitude = (origin_x + columns * pixel_width).astype("<f4")
    latitude = (origin_y + lines * pixel_height).astype("<f4")

    # Row-major: all of row 0's columns, then row 1's. Matches C-order ravel of
    # the (rows, cols) value arrays.
    latitude_grid = np.repeat(latitude, cols)
    longitude_grid = np.tile(longitude, rows)

    return Grid(latitude=latitude_grid, longitude=longitude_grid, rows=rows, cols=cols)
