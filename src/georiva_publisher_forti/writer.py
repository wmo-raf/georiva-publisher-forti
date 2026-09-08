"""Turning a run into Forti's bytes. Never into its markers.

The layout, from `forti-internalformat`::

    {org}/forti/
    ├── latest/<area>                 ← ENGINE writes this, last
    ├── jsonformat.json
    └── <area>/<version>/
        ├── complete.json             ← ENGINE writes this, second to last
        └── <md5(lat||lon)>/
            ├── latitude    float32[n_points]
            ├── longitude   float32[n_points]
            ├── data        int16[n_points x n_values]  little-endian
            └── meta.json

``data`` is **point-major**: for each point, every parameter's whole time series
concatenated, ordered by each parameter's ``slice_from``. A reader finds a value
at ``point_index * number_of_points + slice_from + step`` — where
``number_of_points`` is, confusingly, *values per location* and not the number of
locations.

This module writes none of the markers. It stages the bytes and hands the caller
the markers to publish; the sink refuses them from here anyway, which is the point
— the ordering rule is enforced rather than remembered. See
``core.publishing.markers``.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC

import numpy as np

from georiva.core.publishing import CompletionMarker

logger = logging.getLogger(__name__)

INT16_MIN, INT16_MAX = -32768, 32767


@dataclass
class Series:
    """One parameter's values, ready to pack.

    ``values`` is ``(time, point)`` and ``times`` are the valid times — the
    window *ends* for a period parameter, because jsonfrontend files a value
    under ``end - offset``.
    """

    parameter: object
    times: list
    values: np.ndarray


def pack(series: Series) -> np.ndarray:
    """int16, little-endian, at the parameter's own scale factor.

    Two refusals rather than two silent corruptions. A NaN would encode as
    ``-32768`` and decode as a perfectly plausible ``-3276.8``, and the format has
    no missing-value convention anywhere to say otherwise. An overflow would wrap
    into a real-looking value at the other end of the range.
    """
    scaled = np.round(series.values / series.parameter.scale_factor)

    if np.isnan(scaled).any():
        count = int(np.isnan(scaled).sum())
        raise ValueError(
            f"{series.parameter.name}: {count} value(s) are NaN. Forti has no missing-value "
            f"convention — this would encode as -32768 and decode as -3276.8, which is a "
            f"number a reader will happily believe."
        )

    low, high = float(scaled.min()), float(scaled.max())
    if low < INT16_MIN or high > INT16_MAX:
        raise ValueError(
            f"{series.parameter.name}: scale factor {series.parameter.scale_factor} puts values "
            f"in {low:.0f}..{high:.0f}, outside int16. Raise the scale factor."
        )

    return scaled.astype("<i2")


def build_meta(all_series: list[Series]) -> tuple[dict, list[np.ndarray]]:
    """``meta.json`` and the packed arrays, in matching ``slice_from`` order."""
    parameters = {}
    packed = []
    slice_from = 0

    for series in all_series:
        parameters[series.parameter.name] = {
            "units": series.parameter.units,
            "times": [_iso(time) for time in series.times],
            "slice_from": slice_from,
            # Always present. A zero or missing factor decodes as 0.1, which
            # turns symbol code 46 into 4.6 and then into no symbol at all.
            "scale_factor": series.parameter.scale_factor,
        }
        slice_from += len(series.times)
        packed.append(pack(series))

    return {"parameters": parameters, "number_of_points": slice_from}, packed


def point_major(packed: list[np.ndarray], point_count: int) -> bytes:
    """The ``data`` blob: every parameter's series for point 0, then point 1, …"""
    block = np.concatenate([array.T for array in packed], axis=1)
    if block.shape[0] != point_count:
        raise ValueError(f"packed {block.shape[0]} points, grid has {point_count}")
    return block.astype("<i2").tobytes(order="C")


def stage_version(sink, area: str, version: int, grid, all_series: list[Series]) -> dict:
    """Write one version's bytes. Returns what the markers will need.

    Everything here is an ordinary object, so the sink accepts all of it. The two
    markers — ``complete.json`` and ``latest/<area>`` — are built by
    ``completion_markers`` and written afterwards, by the engine.
    """
    meta, packed = build_meta(all_series)
    prefix = f"{area}/{version}/{grid.identifier}"

    written = [
        sink.write(f"{prefix}/latitude", grid.latitude.tobytes()),
        sink.write(f"{prefix}/longitude", grid.longitude.tobytes()),
        sink.write(f"{prefix}/data", point_major(packed, grid.point_count)),
        sink.write_json(f"{prefix}/meta.json", meta),
    ]

    logger.info(
        "staged %s v%d: %d point(s), %d value(s) per point, %d object(s)",
        area,
        version,
        grid.point_count,
        meta["number_of_points"],
        len(written),
    )

    return {
        "prefix": prefix,
        "objects": written,
        "values_per_point": meta["number_of_points"],
        "witness": f"{prefix}/data",
    }


def completion_markers(area: str, version: int, time_until_next=None) -> list[CompletionMarker]:
    """The two markers, in the order they must be written.

    ``complete.json`` first: it is the manifest ``latest/<area>`` points at, and a
    reader that finds the pointer follows it immediately. ``latest/<area>`` second:
    it is the actual load trigger, polled every 3 s (`forecast.go:126`) — not
    ``complete.json``, whatever the internalformat README says.

    ``geographic_extent`` is null on purpose. A non-null one is what the GEOS
    polygon leak needs, and coverage is bounded by ``maximum_gridpoint_distance``
    instead.
    """
    complete = {
        "area": area,
        "version": version,
        "geographic_extent": None,
    }
    if time_until_next is not None:
        # Go's time.Duration is nanoseconds.
        complete["time_until_next"] = int(time_until_next.total_seconds() * 1_000_000_000)

    return [
        CompletionMarker(
            f"{area}/{version}/complete.json",
            json.dumps(complete, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8"),
        ),
        CompletionMarker(f"latest/{area}", str(version).encode("utf-8")),
    ]


def _iso(time) -> str:

    return time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
