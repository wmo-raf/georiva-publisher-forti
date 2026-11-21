"""Reading one bbox out of many COGs, cheaply, through core's read module.

The whole reason this plugin exists is that a point query against the COGs is
expensive: one COG per variable per timestep, global 0.25°, 1440x721, 256x256
tiles at about 1.76 MB each. Answering a single coordinate costs ~950 range reads
and ~93 MB across 14 API calls.

The publish side gets to do the opposite of what a point query does. Kenya at
0.25° falls inside a *single* 256x256 tile (pixel x 855-968, y 339-379), so
reading the whole area out of one COG is one windowed read of about 96 KB — not
the 1.76 MB the object weighs. Roughly 750 of those, once per run.

That economy only exists if the read is windowed, and core's
``values.Session.window`` is: it opens the asset for range reads (an fsspec
opener on our bucket, ``/vsicurl/`` for a Remote COG) and reads only the blocks
under the box. Every value is read through it (ADR 0046 §10, §20) — in the unit
the caller names, because the bytes are in the Variable's source unit and Forti
publishes a fixed unit per parameter (§11).

**The point list is the window core read.** Forti addresses every value by its
row-major position in the point list, so the list is built from the
:class:`~georiva.core.values.Field` the first read returns — its transform and
shape — and not from a second computation of "which pixels does this box
cover" that could round an edge the other way. Every later read must return a
field of exactly that transform, shape and CRS, or it is refused: a different
window would file values under coordinates they do not belong to.
"""

import numpy as np

from georiva.core import values

from .grid import Grid, from_raster_window

#: Forti's points are longitude and latitude; a field in any other CRS has
#: coordinates the point list cannot carry.
WGS84 = "EPSG:4326"


class ZeroField:
    """A field that is zero everywhere, read in place of an asset nobody wrote.

    What a slot accumulated from the run's start holds at the run's start, by
    definition — and what a store that holds no step there (AIFS) leaves Forti
    to supply. The planner decides where one stands (:func:`~.planner.readable_assets`);
    this reads it as the window every other read returns, zero in any unit.
    """

    href = "(zero at the run's start)"

    def __repr__(self):
        return "ZERO"


#: The one zero field; a plan names it where an asset would stand.
ZERO = ZeroField()


class WindowReader:
    """Reads the same window out of a series of assets, within one reading session."""

    def __init__(self, session, bbox):
        self.session = session
        self.bbox = tuple(bbox)
        #: ``(transform, shape, crs)`` of the window every read must return.
        self.geometry = None
        self.grid: Grid | None = None

    def resolve(self, asset) -> Grid:
        """Read one asset's window to fix the geometry; returns the point list.

        Read as stored: the geometry does not depend on the unit."""
        west, south, east, north = self.bbox
        if west >= east or south >= north:
            raise ValueError(f"bbox must be (west, south, east, north) with west<east and south<north, got {self.bbox}")
        field = self.session.window(asset, self.bbox, in_unit=values.SOURCE_UNIT)
        if field.crs != WGS84:
            raise ValueError(
                f"{asset.href} is in {field.crs}, not {WGS84}. Forti's points are longitude and latitude, "
                f"so only a grid in {WGS84} can be published."
            )
        if field.data.size == 0:
            raise ValueError(
                f"bbox {self.bbox} does not overlap the raster of {asset.href}. "
                f"An area with no points resolves nothing."
            )
        self.geometry = _geometry(field)
        self.grid = from_raster_window(tuple(field.transform)[:6], field.height, field.width, 0, 0)
        return self.grid

    def read(self, asset, in_unit: str) -> np.ndarray:
        """The window from one asset in ``in_unit``, as a ``(rows, cols)`` float32 array.

        :data:`ZERO` reads as zeros over the window the other reads fixed: it
        has no geometry of its own, so it cannot be the read that fixes one."""
        if asset is ZERO:
            if self.geometry is None:
                raise ValueError("A zero field has no geometry; read an asset first to fix the window.")
            return np.zeros(self.geometry[1], dtype=np.float32)
        if self.geometry is None:
            self.resolve(asset)
        field = self.session.window(asset, self.bbox, in_unit=in_unit)
        if _geometry(field) != self.geometry:
            raise ValueError(
                f"{asset.href} has a different raster geometry from the rest of the run. "
                f"Every value in a Forti area is addressed by point ordinal, so a "
                f"shifted grid files values under the wrong coordinates."
            )
        return field.data

    def read_series(self, assets, in_unit: str) -> np.ndarray:
        """One variable's whole run over the window, in ``in_unit``, as ``(time, point)``.

        Flattened row-major, matching ``grid.from_raster_window``'s ordering —
        the point ordinal is the only thing tying a value to a place.
        """
        frames = [self.read(asset, in_unit).ravel(order="C") for asset in assets]
        return np.stack(frames)


def _geometry(field):
    return tuple(field.transform)[:6], field.data.shape, field.crs
