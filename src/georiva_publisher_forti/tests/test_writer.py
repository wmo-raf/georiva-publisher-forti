"""The bytes, and the two markers that are not among them.

Everything the writer produces is an ordinary object. The markers — the manifest
and the pointer — are built here but written by the engine, after the bytes they
promise, and the sink refuses them from any other door.

The packing refusals are not defensive programming. The format has no
missing-value convention anywhere, so a NaN encodes as -32768 and decodes as
-3276.8: a number a reader believes.
"""

import json
from datetime import UTC, datetime, timedelta

import numpy as np
from django.test import SimpleTestCase

from georiva.core.publishing import MarkerOrderingError
from georiva_publisher_forti import parameters as params
from georiva_publisher_forti.writer import (
    Series,
    build_meta,
    completion_markers,
    pack,
    point_major,
)

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)

#: What the writer is handed: ``{org}.{area}``, one path segment. Spelled out
#: here rather than derived, because the shape is the thing under test.
AREA_KEY = "central.kenya"

TEMPERATURE = params.BY_NAME["air_temperature_2m"]
SYMBOL = params.BY_NAME["weather_symbol_6h"]


def times(count, hours=3):
    return [BASE + timedelta(hours=hours * step) for step in range(count)]


class PackingTests(SimpleTestCase):
    def test_values_are_little_endian_int16_at_the_parameters_scale(self):
        series = Series(TEMPERATURE, times(2), np.array([[24.3], [24.8]]))

        packed = pack(series)

        self.assertEqual(packed.dtype.str, "<i2")
        self.assertEqual(packed.ravel().tolist(), [243, 248])

    def test_a_nan_is_refused_rather_than_encoded(self):
        series = Series(TEMPERATURE, times(2), np.array([[24.3], [np.nan]]))

        with self.assertRaises(ValueError) as ctx:
            pack(series)

        self.assertIn("-3276.8", str(ctx.exception))

    def test_an_overflow_is_refused_rather_than_wrapped(self):
        series = Series(TEMPERATURE, times(1), np.array([[5000.0]]))

        with self.assertRaises(ValueError) as ctx:
            pack(series)

        self.assertIn("int16", str(ctx.exception))

    def test_the_symbol_rounds_trips_exactly_at_scale_factor_one(self):
        """A zero or missing factor decodes as 0.1, which turns code 46 into 4.6
        and then into no symbol at all (`values.go:31`)."""
        series = Series(SYMBOL, times(2), np.array([[46.0], [174.0]]))

        packed = pack(series)

        self.assertEqual(packed.ravel().tolist(), [46, 174])
        self.assertEqual(SYMBOL.scale_factor, 1.0)


class MetaTests(SimpleTestCase):
    def _meta(self):
        return build_meta(
            [
                Series(TEMPERATURE, times(3), np.zeros((3, 2))),
                Series(SYMBOL, times(2), np.zeros((2, 2))),
            ]
        )

    def test_slice_from_accumulates_over_the_parameters_in_order(self):
        meta, _ = self._meta()

        self.assertEqual(meta["parameters"]["air_temperature_2m"]["slice_from"], 0)
        self.assertEqual(meta["parameters"]["weather_symbol_6h"]["slice_from"], 3)

    def test_number_of_points_is_values_per_location_not_locations(self):
        """Forti's own confusing name: it is the stride into ``data``."""
        meta, _ = self._meta()

        self.assertEqual(meta["number_of_points"], 5)

    def test_every_parameter_states_its_scale_factor(self):
        meta, _ = self._meta()

        for entry in meta["parameters"].values():
            self.assertIn("scale_factor", entry)
            self.assertNotEqual(entry["scale_factor"], 0)

    def test_times_are_utc_iso_with_a_trailing_z(self):
        meta, _ = self._meta()

        self.assertEqual(meta["parameters"]["air_temperature_2m"]["times"][0], "2026-09-02T12:00:00Z")


class DataLayoutTests(SimpleTestCase):
    def test_data_is_point_major(self):
        """For each point, every parameter's whole series — which is what makes
        a point query one read."""
        temperature = np.array([[1, 2], [3, 4], [5, 6]], dtype="<i2")  # (time, point)
        symbol = np.array([[7, 8]], dtype="<i2")

        blob = point_major([temperature, symbol], point_count=2)
        values = np.frombuffer(blob, dtype="<i2")

        # point 0: its whole temperature series, then its symbol
        self.assertEqual(values[:4].tolist(), [1, 3, 5, 7])
        # point 1: the same
        self.assertEqual(values[4:].tolist(), [2, 4, 6, 8])

    def test_a_point_count_mismatch_is_refused(self):
        with self.assertRaises(ValueError):
            point_major([np.zeros((3, 2), dtype="<i2")], point_count=5)

    def test_a_reader_finds_a_value_by_ordinal_and_slice_from(self):
        """The arithmetic ``values.Read`` does: point * number_of_points +
        slice_from + step."""
        temperature = np.array([[10, 20], [11, 21], [12, 22]], dtype="<i2")
        symbol = np.array([[46, 47]], dtype="<i2")
        meta, packed = build_meta(
            [
                Series(TEMPERATURE, times(3), temperature.astype(float) / 10),
                Series(SYMBOL, times(1), symbol.astype(float)),
            ]
        )
        values = np.frombuffer(point_major(packed, point_count=2), dtype="<i2")

        stride = meta["number_of_points"]
        slice_from = meta["parameters"]["weather_symbol_6h"]["slice_from"]

        self.assertEqual(int(values[1 * stride + slice_from]), 47)


class CompletionMarkerTests(SimpleTestCase):
    def test_the_manifest_comes_before_the_pointer_that_names_it(self):
        markers = completion_markers(AREA_KEY, 178835040000)

        self.assertEqual(
            [marker.path for marker in markers],
            [f"{AREA_KEY}/178835040000/complete.json", f"latest/{AREA_KEY}"],
        )

    def test_the_manifest_names_the_area_the_reader_asked_for(self):
        """``complete.json``'s own ``area`` is the key, not the publication's
        name: the key is what the reader was configured with."""
        manifest = json.loads(completion_markers(AREA_KEY, 1)[0].content)

        self.assertEqual(manifest["area"], AREA_KEY)

    def test_the_pointer_holds_the_version_and_nothing_else(self):
        markers = completion_markers(AREA_KEY, 178835040000)

        self.assertEqual(markers[1].content, b"178835040000")

    def test_the_manifest_carries_a_null_geographic_extent(self):
        """A non-null one is what the GEOS polygon leak needs; coverage is
        bounded by maximum_gridpoint_distance instead."""
        manifest = json.loads(completion_markers(AREA_KEY, 1)[0].content)

        self.assertIsNone(manifest["geographic_extent"])

    def test_time_until_next_is_nanoseconds_because_go_reads_a_duration(self):
        manifest = json.loads(completion_markers(AREA_KEY, 1, timedelta(hours=12))[0].content)

        self.assertEqual(manifest["time_until_next"], 12 * 3600 * 1_000_000_000)

    def test_it_is_omitted_when_there_is_nothing_to_measure(self):
        manifest = json.loads(completion_markers(AREA_KEY, 1)[0].content)

        self.assertNotIn("time_until_next", manifest)


class MarkersAreNotWriterOutputTests(SimpleTestCase):
    """The sink refuses them, so the ordering rule is enforced rather than
    remembered."""

    def test_the_writer_cannot_stage_a_marker(self):
        from georiva_publisher_forti.models import instance_sink

        sink = instance_sink()

        for path in ("latest/central.kenya", "central.kenya/1/complete.json"):
            with self.subTest(path=path), self.assertRaises(MarkerOrderingError):
                sink.write(path, b"x")
