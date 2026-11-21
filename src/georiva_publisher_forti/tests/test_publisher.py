"""A whole publish, from COGs on disk to the marker that makes it real.

Nothing is stubbed between the rasters and the bytes: real GeoTIFFs go in, the
window is read through rasterio, the derivations run, and the tree comes out on a
real filesystem-backed sink. What the test then does is what
``rawdataforecaster`` does — reads the coordinates, reads ``meta.json``, and
finds a value by ``point * number_of_points + slice_from + step``. If that
arithmetic disagrees, the format is wrong regardless of what the writer thought.

The ordering property is the one that cannot be checked by reading the code: the
markers must not exist while the bytes are still going down. M0 established that
``rawdataforecaster`` really does wait — four seconds of staged bytes with the
marker withheld drew no load attempt — so what is left to hold is that we
actually stage first.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import numpy as np
from django.test import TestCase

from georiva.core.models import Asset, Unit, Variable
from georiva.core.publishing import MarkerOrderingError
from georiva.harvest.testing import LocalStore, StoreArray, StoreVariable
from georiva.ingestion.models import RunIngestion
from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG, make_organisation
from georiva_publisher_forti import config, publisher, readiness, tasks
from georiva_publisher_forti.models import FortiPublication
from georiva_publisher_forti.planner import PublicationRefused, plan
from georiva_publisher_forti.publisher import GridMoved, publish

from .assets_on_disk import AssetsOnDiskMixin
from .factories import REFERENCE_TIME, make_collection, make_publication, make_run, write_cogs
from .sink_isolation import TemporarySinkMixin


class PublishTestCase(AssetsOnDiskMixin, TemporarySinkMixin, TestCase):
    def setUp(self):
        # The two seams between this plugin and object storage: the assets
        # bucket core reads values from, and the sink publications go to.
        self.isolate_sink()
        self.cogs = self.isolate_assets()

        self.collection = make_collection()
        self.publication = make_publication(self.collection)
        # Every path below is derived from the key, never spelled: the key is
        # `{org}.{area}` and the organisation is generated per test.
        self.area_key = self.publication.area_key
        write_cogs(self.collection, self.cogs, step_hours=3, steps=9)
        make_run(self.collection)

    def sink(self):
        return self.publication.sink()

    def reread(self):
        return FortiPublication.objects.get(pk=self.publication.pk)


class EndToEndTests(PublishTestCase):
    def test_a_publish_writes_a_loadable_version(self):
        result = publish(self.publication)

        self.assertFalse(result["skipped"])
        self.assertEqual(result["points"], 25)
        self.assertEqual(result["steps"], 9)

    def test_the_pointer_names_the_version_that_was_written(self):
        result = publish(self.publication)
        sink = self.sink()

        pointed_at = int(sink.read_bytes(f"latest/{self.area_key}").decode())

        self.assertEqual(pointed_at, result["version"])
        self.assertTrue(sink.exists(f"{self.area_key}/{pointed_at}/complete.json"))

    def test_the_tree_has_the_four_objects_a_reader_opens(self):
        publish(self.publication)
        sink = self.sink()
        grid_id = self.reread().grid_id

        for name in ("latitude", "longitude", "data", "meta.json"):
            with self.subTest(object=name):
                self.assertTrue(sink.exists(f"{self.area_key}/{self.reread().published_version}/{grid_id}/{name}"))

    def test_the_grid_directory_is_the_md5_of_the_coordinates(self):
        import hashlib

        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        latitude = sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/latitude")
        longitude = sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/longitude")

        self.assertEqual(hashlib.md5(latitude + longitude).hexdigest(), grid_id)

    def test_a_reader_finds_the_temperature_it_was_given(self):
        """The arithmetic ``values.Read`` does, against a known constant."""
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/data"), dtype="<i2")

        entry = meta["parameters"]["air_temperature_2m"]
        stride = meta["number_of_points"]
        at_point_7_step_0 = values[7 * stride + entry["slice_from"]]

        self.assertAlmostEqual(float(at_point_7_step_0) * entry["scale_factor"], 24.0, places=5)

    def test_a_variable_stored_in_kelvin_is_published_in_celsius(self):
        """The COGs hold their Variable's source unit (ADR 0046 §1); the slot
        publishes degC, and the read converts — 297.15 K is 24 °C."""
        Variable.objects.filter(collection=self.collection, slug="2t").update(source_unit=self.kelvin())
        write_cogs(self.collection, self.cogs, step_hours=3, steps=9, constants={"2t": 297.15})
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/data"), dtype="<i2")

        entry = meta["parameters"]["air_temperature_2m"]
        at_point_7_step_0 = values[7 * meta["number_of_points"] + entry["slice_from"]]

        self.assertEqual(entry["units"], "celsius")
        self.assertAlmostEqual(float(at_point_7_step_0) * entry["scale_factor"], 24.0, places=5)

    def kelvin(self):
        unit, _ = Unit.objects.get_or_create(symbol="K", defaults={"name": "Kelvin"})
        return unit

    def test_the_precipitation_window_is_what_fell_over_six_hours(self):
        """``tp`` accumulates by 0.5 mm per 3-hour step, so a 6-hour window is
        1.0 mm — and finding it needs two steps, not one."""
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/data"), dtype="<i2")

        entry = meta["parameters"]["precipitation_amount_acc6h"]
        stride = meta["number_of_points"]
        first = values[0 * stride + entry["slice_from"]]

        self.assertAlmostEqual(float(first) * entry["scale_factor"], 1.0, places=5)

    def test_a_period_parameters_times_are_window_ends(self):
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/meta.json"))
        instants = meta["parameters"]["air_temperature_2m"]["times"]
        windows = meta["parameters"]["precipitation_amount_acc6h"]["times"]

        # The first 6-hour window ends at the third instant of a 3-hourly run.
        self.assertEqual(windows[0], instants[2])

    def test_the_run_publishes_no_one_hour_series(self):
        publish(self.publication)

        self.assertNotIn("precipitation_amount_acc1h", self.reread().published_parameters)
        self.assertIn("weather_symbol_6h", self.reread().published_parameters)

    def test_the_publication_records_what_it_published(self):
        result = publish(self.publication)
        publication = self.reread()

        self.assertEqual(publication.status, FortiPublication.Status.READY)
        self.assertEqual(publication.published_version, result["version"])
        self.assertEqual(publication.point_count, 25)
        self.assertEqual(publication.published_step_count, 9)
        self.assertTrue(publication.grid_id)

    def test_the_log_line_says_how_long_the_build_took(self):
        with self.assertLogs("georiva_publisher_forti.publisher", level="INFO") as logged:
            publish(self.publication)

        (line,) = [message for message in logged.output if "published version" in message]
        self.assertRegex(line, r", in \d+\.\d s$")


class MarkerOrderingTests(PublishTestCase):
    def test_no_marker_exists_while_the_bytes_are_still_going_down(self):
        """The property M0 proved a reader depends on. Checked by looking at the
        bucket at the moment the last ordinary object is written.

        The *staged* objects, which is every write under the area key. The config
        documents are written through the same door and deliberately after the
        marker — they advertise what the marker made real — so they are not
        counted here; ``ConfigOrderingTests`` is where their ordering is checked,
        from the other side.
        """
        seen = {}
        sink = self.sink()
        original_write = type(sink).write

        def watched_write(self_sink, relpath, content):
            key = original_write(self_sink, relpath, content)
            if relpath.startswith(f"{self.area_key}/"):
                seen[relpath] = self_sink.exists(f"latest/{self.area_key}")
            return key

        type(sink).write = watched_write
        self.addCleanup(setattr, type(sink), "write", original_write)

        publish(self.publication)

        self.assertTrue(seen, "no ordinary objects were staged")
        self.assertFalse(
            any(seen.values()),
            "latest/<area> existed while bytes were still being written — a reader "
            "polling at that moment loads a partial dataset",
        )

    def test_a_failure_to_stage_leaves_no_marker_behind(self):
        sink = self.sink()
        original_write = type(sink).write

        def failing_write(self_sink, relpath, content):
            if relpath.endswith("/data"):
                raise OSError("storage went away")
            return original_write(self_sink, relpath, content)

        type(sink).write = failing_write
        self.addCleanup(setattr, type(sink), "write", original_write)

        with self.assertRaises(OSError):
            publish(self.publication)

        self.assertFalse(self.sink().exists(f"latest/{self.area_key}"))

    def test_the_engine_refuses_markers_over_bytes_that_are_not_there(self):
        sink = self.sink()

        with self.assertRaises(MarkerOrderingError):
            from georiva_publisher_forti.writer import completion_markers

            sink.publish_markers(
                completion_markers(self.area_key, 1),
                require_staged=[f"{self.area_key}/1/grid/data"],
            )


class RepublishTests(PublishTestCase):
    def test_an_unchanged_run_is_skipped(self):
        publish(self.publication)

        self.assertTrue(publish(self.reread())["skipped"])

    def test_a_reopened_run_republishes_at_a_higher_version(self):
        first = publish(self.publication)

        run = self.collection.run_ingestions.get()
        run.revision += 1
        run.save(update_fields=["revision"])
        second = publish(self.reread())

        self.assertGreater(second["version"], first["version"])
        self.assertEqual(int(self.sink().read_bytes(f"latest/{self.area_key}").decode()), second["version"])

    def _republish_at_next_generation(self):
        """Raise the generation by one and publish the same run again.

        The two-step an operator performs, and the whole of what #9 makes
        possible: nothing about the run has moved — same reference time, same
        revision, same steps, same parameters — so the generation is the only
        thing that can carry the new bytes past the reader's strictly-greater
        test.
        """
        publication = self.reread()
        publication.generation += 1
        publication.save(update_fields=["generation"])
        publication.mark_stale()
        return publish(self.reread())

    def test_raising_the_generation_republishes_the_same_run_at_a_greater_stamp(self):
        """The failure this exists to remove, end to end.

        The bytes must reach the bucket under a *strictly greater* integer,
        because ``forecast.go:293`` is what decides whether they are ever read.
        """
        first = publish(self.publication)

        second = self._republish_at_next_generation()

        self.assertFalse(second["skipped"])
        self.assertGreater(second["version"], first["version"])
        self.assertEqual(
            int(self.sink().read_bytes(f"latest/{self.area_key}").decode()),
            second["version"],
        )

    def test_remapping_a_slot_reaches_the_bucket_at_a_greater_stamp(self):
        """The whole of #12, end to end and with no user interface.

        Nothing about the run moves — same reference time, same revision, same
        steps — and the bytes are different because a slot now reads a different
        variable. The edit raises the generation by itself, so the pointer the
        reader polls moves without anybody performing the two-step.
        """
        first = publish(self.publication)

        row = self.reread().variable_mappings.get(slot="2d")
        row.variable = self.collection.variables.get(slug="2t")
        row.save()

        second = publish(self.reread())

        self.assertFalse(second["skipped"])
        self.assertGreater(second["version"], first["version"])
        self.assertEqual(
            int(self.sink().read_bytes(f"latest/{self.area_key}").decode()),
            second["version"],
        )

    def test_a_remapped_slot_publishes_the_variable_it_was_given(self):
        """The stamp moving is only half of it — the dew point series has to
        actually hold what the temperature variable holds, or the publication
        advertises a correction it did not make."""
        publish(self.publication)
        row = self.reread().variable_mappings.get(slot="2d")
        row.variable = self.collection.variables.get(slug="2t")
        row.save()

        version = publish(self.reread())["version"]

        self.assertEqual(
            self._first_value(version, "dew_point_temperature_2m"),
            self._first_value(version, "air_temperature_2m"),
        )

    def _first_value(self, version, parameter):
        """One parameter's first value at the first point, as the reader finds
        it: ``point * number_of_points + slice_from + step``."""
        sink = self.sink()
        prefix = f"{self.area_key}/{version}"
        grid = next(name for name in sink.children(prefix))
        meta = json.loads(sink.read_bytes(f"{prefix}/{grid}/meta.json").decode())
        entry = meta["parameters"][parameter]
        data = np.frombuffer(sink.read_bytes(f"{prefix}/{grid}/data"), dtype="<i2")
        return data[entry["slice_from"]] * entry["scale_factor"]

    def test_the_raised_generation_leaves_a_second_version_directory(self):
        """The pointer moving is only half of it — the bytes it names have to be
        somewhere the reader can fetch them from."""
        first = publish(self.publication)

        second = self._republish_at_next_generation()

        sink = self.sink()
        self.assertTrue(sink.exists(f"{self.area_key}/{first['version']}/complete.json"))
        self.assertTrue(sink.exists(f"{self.area_key}/{second['version']}/complete.json"))

    def test_a_new_run_resets_the_stored_generation(self):
        """The reset has to reach the row, not only the plan: an operator who
        raised it to 1 and then saw a new run publish must find the field back
        at 0, or the next raise starts from a number that names nothing.

        A genuinely later reference time, not a revision bump — ADR 0003 counts
        those as different things, and only this one is "a new run".
        """
        publish(self.publication)
        self._republish_at_next_generation()
        self.assertEqual(self.reread().generation, 1)

        later = REFERENCE_TIME + timedelta(hours=12)
        write_cogs(self.collection, self.cogs, step_hours=3, steps=9, reference_time=later)
        make_run(self.collection, reference_time=later)
        self.publication.mark_stale()
        publish(self.reread())

        self.assertEqual(self.reread().published_reference_time, later)
        self.assertEqual(self.reread().generation, 0)

    def test_a_republished_run_also_resets_the_stored_generation(self):
        """A reopened run is not the run the generation was raised against
        either: the revision above it has moved, so generation 0 already
        outranks whatever it had reached."""
        publish(self.publication)
        self._republish_at_next_generation()

        run = self.collection.run_ingestions.get()
        run.revision += 1
        run.save(update_fields=["revision"])
        self.publication.mark_stale()
        publish(self.reread())

        self.assertEqual(self.reread().generation, 0)

    def test_a_grid_that_moved_is_refused_rather_than_published(self):
        """Every stored value is addressed by an ordinal into the point list, and
        rawdataforecaster caches its s2 index under the list's MD5."""
        publish(self.publication)

        publication = self.reread()
        publication.east = publication.east + 0.5
        publication.save(update_fields=["east"])
        publication.mark_stale()

        with self.assertRaises(GridMoved):
            publish(self.reread())


class RetentionTests(PublishTestCase):
    def _publish_versions(self, count):
        run = self.collection.run_ingestions.get()
        versions = []
        for _ in range(count):
            versions.append(publish(self.reread())["version"])
            run.revision += 1
            run.save(update_fields=["revision"])
        return versions

    def _publish_runs(self, count):
        """Publish ``count`` runs, each a revision of the last, with no trailing
        bump — so the next generation raised is one of the newest run."""
        run = self.collection.run_ingestions.get()
        versions = []
        for index in range(count):
            if index:
                run.revision += 1
                run.save(update_fields=["revision"])
                self.publication.mark_stale()
            versions.append(publish(self.reread())["version"])
        return versions

    def _publish_generations(self, count):
        """Remap the newest run ``count`` times, as an operator correcting a mapping would."""
        versions = []
        for _ in range(count):
            publication = self.reread()
            publication.generation += 1
            publication.save(update_fields=["generation"])
            publication.mark_stale()
            versions.append(publish(self.reread())["version"])
        return versions

    def test_remaps_never_evict_the_run_a_bad_run_rolls_back_to(self):
        """Counted in versions, four remaps of one run would fill every slot and
        prune the run before it — the only thing to point a reader back to once
        that run's data turns out to be wrong, because every generation of it
        carries the same wrong data (ADR 0009)."""
        previous, newest = self._publish_runs(2)
        generations = self._publish_generations(publisher.RUNS_KEPT)
        sink = self.sink()

        self.assertTrue(sink.exists(f"{self.area_key}/{previous}/complete.json"))
        for version in [newest, *generations]:
            self.assertTrue(sink.exists(f"{self.area_key}/{version}/complete.json"))

    def test_a_run_past_retention_goes_with_every_generation_of_it(self):
        """Generations ride with their run, out as well as in: a superseded run
        leaves no stray generation behind to be counted against the rest."""
        oldest = self._publish_runs(1)
        oldest += self._publish_generations(2)
        run = self.collection.run_ingestions.get()
        for _ in range(publisher.RUNS_KEPT):
            run.revision += 1
            run.save(update_fields=["revision"])
            self.publication.mark_stale()
            publish(self.reread())
        sink = self.sink()

        for version in oldest:
            self.assertFalse(sink.exists(f"{self.area_key}/{version}/complete.json"))
        self.assertEqual(len(sink.children(self.area_key)), publisher.RUNS_KEPT)

    def test_only_the_newest_versions_survive(self):
        self._publish_versions(publisher.RUNS_KEPT + 2)

        remaining = self.sink().children(self.area_key)

        self.assertEqual(len(remaining), publisher.RUNS_KEPT)

    def test_retention_never_reaches_past_this_publication_s_own_area(self):
        """The root is shared now. A prune that took a prefix by name rather than
        by area key would cost every organisation at once, and nothing downstream
        would report it — the readers would simply stop finding data.

        Core refuses only ``delete_prefix("")``; it cannot refuse ``"latest"`` or
        ``"config"``, because which names under the root are areas is this
        plugin's grammar. So this is the plugin's guarantee to hold.
        """
        sink = self.sink()
        sink.write("other-org.ecmwf-ifs/1/abc/data", b"another tenant's bytes")
        sink.write("jsonformat.json", b"{}")

        self._publish_versions(publisher.RUNS_KEPT + 2)

        self.assertTrue(sink.exists("other-org.ecmwf-ifs/1/abc/data"))
        self.assertTrue(sink.exists("jsonformat.json"))

    def test_a_pruned_version_takes_its_own_manifest_with_it(self):
        """``complete.json`` is a completion marker, and a version directory that
        cannot lose it never goes away."""
        versions = self._publish_versions(publisher.RUNS_KEPT + 1)
        sink = self.sink()

        self.assertFalse(sink.exists(f"{self.area_key}/{versions[0]}/complete.json"))

    def test_the_version_a_reader_is_following_is_never_pruned(self):
        versions = self._publish_versions(publisher.RUNS_KEPT + 1)
        sink = self.sink()

        current = int(sink.read_bytes(f"latest/{self.area_key}").decode())

        self.assertEqual(current, versions[-1])
        self.assertTrue(sink.exists(f"{self.area_key}/{current}/complete.json"))


class ConfigOrderingTests(PublishTestCase):
    """The config is written last, and "last" has two halves.

    After the markers, because it advertises bytes and a listing that runs ahead
    of them is a load failure. And after ``mark_ready``, because
    ``rdfconfig.servable`` reads ``published_version`` and only ``mark_ready``
    sets it — a refresh landing between the marker and that write produces an
    area list that omits the very run that triggered it, with nothing anywhere
    reporting it.
    """

    def test_a_publish_leaves_the_area_in_the_config_it_wrote(self):
        publish(self.publication)

        areas = json.loads(self.sink().read_bytes(config.RAWDATAFORECASTER_PATH).decode("utf-8"))["areas"]

        self.assertEqual(areas, [self.area_key])

    def test_the_refresh_sees_the_marker_and_the_ready_row_already_written(self):
        seen = {}

        def record():
            sink = self.sink()
            seen["marker"] = sink.exists(self.publication.marker_path())
            seen["published_version"] = self.reread().published_version
            return []

        with patch("georiva_publisher_forti.config.refresh", side_effect=record):
            publish(self.publication)

        self.assertTrue(seen["marker"], "the config was refreshed before the marker it advertises")
        self.assertIsNotNone(
            seen["published_version"],
            "the config was refreshed before mark_ready, so servable() would have omitted this run",
        )

    def test_a_config_that_cannot_be_written_does_not_fail_a_good_publish(self):
        """The bytes are on the bucket and latest/<area key> points at them.
        Marking this FAILED would send the sweep to redo work that is done; the
        five-minute reconciler is what an unwritten config is for.
        """
        with patch("georiva_publisher_forti.config.refresh", side_effect=RuntimeError("bucket gone")):
            result = publish(self.publication)

        self.assertFalse(result["skipped"])
        self.assertEqual(self.reread().status, FortiPublication.Status.READY)


# ---- a store-backed collection ----------------------------------------------------


#: The store's arrays, by their path in the store, and the Variable each is
#: harvested into — named after the slot it fills, so the mapping seeds itself.
#: Wind speed and direction, and precipitation as a mass per area marked as water.
STORE_VARIABLES = {
    "temperature_2m": StoreVariable("2t", source_unit="K"),
    "dew_point_temperature_2m": StoreVariable("2d", source_unit="K"),
    "pressure_reduced_to_mean_sea_level": StoreVariable("msl", source_unit="Pa"),
    "wind_speed_10m": StoreVariable("wind_speed_10m", source_unit="m/s"),
    "wind_direction_10m": StoreVariable("wind_dir_10m", source_unit="deg"),
    "total_cloud_cover": StoreVariable("tcc", source_unit="%"),
    "precipitation_surface": StoreVariable("tp", source_unit="kg m-2", water=True),
}

#: What each array holds at every step, in its source unit. Precipitation
#: accumulates from the run's start, 1.5 kg m⁻² per 6-hour step.
STORE_CONSTANTS = {
    "temperature_2m": 297.15,
    "dew_point_temperature_2m": 291.15,
    "pressure_reduced_to_mean_sea_level": 101300.0,
    "wind_speed_10m": 4.5,
    "wind_direction_10m": 120.0,
    "total_cloud_cover": 45.0,
}

#: Inside the store's grid (cell centres 5–75 °E, 15 °S–15 °N, 10° apart).
STORE_BBOX = (10.0, -10.0, 40.0, 10.0)

#: A 6-hourly store, a day long.
STORE_LEADS = [timedelta(hours=hours) for hours in range(0, 30, 6)]


def _constant(value, shape):
    return lambda run, step: np.full(shape, value, dtype=np.float32)


def _accumulating(per_step, shape):
    return lambda run, step: np.full(shape, step * per_step, dtype=np.float32)


class StorePublishTestCase(TemporarySinkMixin, TestCase):
    """A publication on a collection a store harvest made (ADR 0044): its assets
    are Store assets, read out of a real Icechunk store on local disk."""

    def setUp(self):
        self.isolate_sink()
        self.store = LocalStore(self, leads=STORE_LEADS, arrays=self.store_arrays())
        harvested = self.store.harvest(make_organisation(DEFAULT_TEST_ORG_SLUG), variables=self.store_variables())
        self.collection = harvested.collection
        self.collection.is_forecast = True
        self.collection.visibility = self.collection.Visibility.PUBLIC
        self.collection.save(update_fields=["is_forecast", "visibility"])
        self.publication = make_publication(self.collection, bbox=STORE_BBOX)
        self.area_key = self.publication.area_key
        self.reference_time = self.store.runs[-1]

    def store_variables(self):
        return STORE_VARIABLES

    def store_arrays(self):
        shape = (4, 8)
        arrays = [StoreArray(path, values=_constant(value, shape)) for path, value in STORE_CONSTANTS.items()]
        arrays.append(StoreArray("precipitation_surface", values=_accumulating(1.5, shape)))
        return arrays

    def close_run(self):
        """What a store harvest does once the store holds every step of the run."""
        steps = len(self.store.leads)
        return RunIngestion.record_held_steps(
            self.collection, self.reference_time, held=steps, expected=steps, complete=True
        )

    def reread(self):
        return FortiPublication.objects.get(pk=self.publication.pk)

    def published(self):
        """``(meta, values)`` of the version ``latest/<area key>`` points at, as a reader loads them."""
        sink = self.publication.sink()
        version = int(sink.read_bytes(f"latest/{self.area_key}").decode())
        grid_id = self.reread().grid_id
        meta = json.loads(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"{self.area_key}/{version}/{grid_id}/data"), dtype="<i2")
        return meta, values

    def first_value(self, parameter, point=0):
        meta, values = self.published()
        entry = meta["parameters"][parameter]
        return float(values[point * meta["number_of_points"] + entry["slice_from"]]) * entry["scale_factor"]


class PublishFromStoreTests(StorePublishTestCase):
    def test_a_store_backed_run_publishes_a_tree_a_reader_loads(self):
        self.close_run()

        result = publish(self.publication)

        self.assertFalse(result["skipped"])
        self.assertEqual(result["steps"], len(STORE_LEADS))
        self.assertGreater(result["points"], 0)
        self.assertAlmostEqual(self.first_value("wind_speed_10m"), 4.5, places=5)
        self.assertAlmostEqual(self.first_value("wind_from_direction_10m"), 120.0, places=5)
        self.assertAlmostEqual(self.first_value("air_temperature_2m"), 24.0, places=5)

    def test_precipitation_stored_as_water_mass_per_area_publishes_in_millimetres(self):
        """1.5 kg m⁻² of water per 6-hour step is 1.5 mm over a 6-hour window."""
        self.close_run()

        publish(self.publication)

        meta, _values = self.published()
        self.assertEqual(meta["parameters"]["precipitation_amount_acc6h"]["units"], "mm")
        self.assertAlmostEqual(self.first_value("precipitation_amount_acc6h"), 1.5, places=5)

    def test_precipitation_not_marked_as_water_is_refused_naming_the_flag(self):
        Variable.objects.filter(collection=self.collection, slug="tp").update(water=False)
        self.close_run()

        with self.assertRaises(PublicationRefused) as refused:
            publish(self.reread())

        self.assertIn("water", str(refused.exception))

    def test_an_optional_slot_the_store_has_no_array_for_builds_no_series(self):
        """The store has no gusts: the slot stays blank and its series is not built."""
        self.close_run()

        publish(self.publication)

        self.assertIsNone(self.reread().mapped_variables()["10fg"])
        self.assertNotIn("wind_speed_of_gust", self.reread().published_parameters)
        self.assertIn("wind_speed_10m", self.reread().published_parameters)

    def test_a_six_hourly_store_publishes_six_and_twelve_hour_series_and_no_one_hour_ones(self):
        self.close_run()

        publish(self.publication)

        published = self.reread().published_parameters
        self.assertIn("precipitation_amount_acc6h", published)
        self.assertIn("weather_symbol_6h", published)
        self.assertIn("weather_symbol_12h", published)
        self.assertNotIn("precipitation_amount_acc1h", published)
        self.assertNotIn("weather_symbol", published)

    def test_texture_assets_are_never_a_slot_s_values(self):
        """A store harvest's items may carry encoded textures of a variable too;
        the slot's values are its Store assets, whatever else sits on the item."""
        for asset in Asset.objects.filter(item__collection=self.collection, variable__slug="wind_speed_10m"):
            Asset.objects.create(
                item=asset.item,
                variable=asset.variable,
                format=Asset.Format.PNG,
                href=f"textures/{asset.item.time:%Y%m%d%H}.png",
                roles=[Asset.Role.VISUAL],
            )
        self.close_run()

        planned = plan(self.reread())

        formats = {asset.format for assets in planned.assets.values() for asset in assets}
        self.assertEqual(formats, {Asset.Format.ZARR})

    def test_readiness_counts_the_steps_the_build_writes(self):
        self.close_run()

        steps = next(finding for finding in readiness.report(self.reread()).findings if finding.key == "steps")
        result = publish(self.reread())

        self.assertEqual(steps.state, readiness.READY)
        self.assertEqual(steps.answer, f"{result['steps']} steps")


#: A store that holds the 10 m wind as components only, as an Open-Meteo-style
#: store does: its harvest maps one array to one Variable and runs no vector
#: extraction, so there is no speed and no direction to map.
WIND_COMPONENT_VARIABLES = {
    **{path: variable for path, variable in STORE_VARIABLES.items() if not path.startswith("wind_")},
    "wind_u_component_10m": StoreVariable("10u", source_unit="m/s"),
    "wind_v_component_10m": StoreVariable("10v", source_unit="m/s"),
}


class PublishWindFromComponentsTests(StorePublishTestCase):
    """A wind from the north-east, 3 m/s westward and 4 m/s southward: 5 m/s
    from a bearing of 36.87°."""

    def store_variables(self):
        return WIND_COMPONENT_VARIABLES

    def store_arrays(self):
        shape = (4, 8)
        constants = {path: value for path, value in STORE_CONSTANTS.items() if not path.startswith("wind_")}
        constants |= {"wind_u_component_10m": -3.0, "wind_v_component_10m": -4.0}
        arrays = [StoreArray(path, values=_constant(value, shape)) for path, value in constants.items()]
        arrays.append(StoreArray("precipitation_surface", values=_accumulating(1.5, shape)))
        return arrays

    def test_the_mapping_seeds_itself_with_the_components(self):
        mapped = self.publication.mapped_variables()

        self.assertEqual((mapped["10u"].slug, mapped["10v"].slug), ("10u", "10v"))
        self.assertIsNone(mapped["wind_speed_10m"])
        self.assertIsNone(mapped["wind_dir_10m"])

    def test_wind_speed_and_direction_are_derived_from_the_components(self):
        self.close_run()

        publish(self.publication)

        meta, _values = self.published()
        self.assertEqual(meta["parameters"]["wind_speed_10m"]["units"], "m/s")
        self.assertEqual(meta["parameters"]["wind_from_direction_10m"]["units"], "degrees")
        self.assertAlmostEqual(self.first_value("wind_speed_10m"), 5.0, places=5)
        self.assertAlmostEqual(self.first_value("wind_from_direction_10m"), 36.9, places=5)

    def test_the_components_themselves_are_not_published(self):
        """Forti's format has speed and direction; u and v are inputs only."""
        self.close_run()

        publish(self.publication)

        meta, _values = self.published()
        self.assertFalse({"10u", "10v"} & set(meta["parameters"]))
        self.assertIn("wind_speed_10m", self.reread().published_parameters)


class StoreRunSignalTests(StorePublishTestCase):
    """The run-closed signal a store harvest's close emits is the one Forti already handles."""

    def dispatching(self):
        dispatched = []
        self.enterContext(
            patch.object(tasks.publish_forti_area, "delay", side_effect=lambda *args: dispatched.append(args))
        )
        return dispatched

    def test_a_run_closing_as_store_held_dispatches_a_build(self):
        dispatched = self.dispatching()

        with self.captureOnCommitCallbacks(execute=True):
            run = self.close_run()

        self.assertEqual(run.closed_by, RunIngestion.Closer.STORE_HELD)
        self.assertEqual([args[0] for args in dispatched], [self.publication.pk])

    def test_a_reopened_store_run_marks_the_publication_stale_and_builds_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.close_run()
        publish(self.reread())
        dispatched = self.dispatching()

        steps = len(self.store.leads)
        with self.captureOnCommitCallbacks(execute=True):
            RunIngestion.record_held_steps(
                self.collection, self.reference_time, held=steps - 1, expected=steps, complete=False
            )

        self.assertEqual(self.reread().status, FortiPublication.Status.STALE)
        self.assertEqual(dispatched, [])


class PrecipitationStoreTestCase(StorePublishTestCase):
    """A store whose precipitation and temperature hold only the steps a test says."""

    #: Precipitation steps the store does not hold, by step index. Accumulated
    #: from the run's start, a store holds no step there (AIFS).
    precipitation_not_held = (0,)
    #: Temperature steps the store does not hold, by step index.
    temperature_not_held = ()
    #: What precipitation starts from, and accumulates per step, in kg m⁻².
    offset = 0.0
    per_step = 1.5

    def store_arrays(self):
        shape = (4, 8)
        arrays = [
            StoreArray(
                path,
                values=_constant(value, shape),
                held=(lambda run, step: step not in self.temperature_not_held) if path == "temperature_2m" else None,
            )
            for path, value in STORE_CONSTANTS.items()
        ]
        arrays.append(
            StoreArray(
                "precipitation_surface",
                values=lambda run, step: np.full(shape, self.offset + step * self.per_step, dtype=np.float32),
                held=lambda run, step: step not in self.precipitation_not_held,
            )
        )
        return arrays

    def leads(self, times):
        return [time - self.reference_time for time in times]

    def window_ends(self, parameter):
        """The leads a published period series ends at."""
        meta, _values = self.published()
        return self.leads(datetime.fromisoformat(time) for time in meta["parameters"][parameter]["times"])

    def steps_finding(self):
        return next(finding for finding in readiness.report(self.reread()).findings if finding.key == "steps")


def _hours(*hours):
    return [timedelta(hours=hour) for hour in hours]


class PrecipitationAtRunStartTests(PrecipitationStoreTestCase):
    """No precipitation step at the run's start: Forti supplies the zero there."""

    def test_the_first_six_hour_window_is_published_and_equals_the_accumulation_at_six_hours(self):
        self.close_run()

        result = publish(self.publication)

        self.assertEqual(result["steps"], len(STORE_LEADS))
        self.assertEqual(self.window_ends("precipitation_amount_acc6h"), _hours(6, 12, 18, 24))
        self.assertAlmostEqual(self.first_value("precipitation_amount_acc6h"), 1.5, places=5)

    def test_the_first_window_s_weather_symbols_are_published(self):
        self.close_run()

        publish(self.publication)

        self.assertEqual(self.window_ends("weather_symbol_6h"), _hours(6, 12, 18, 24))

    def test_readiness_counts_the_steps_the_build_writes(self):
        self.close_run()

        steps = self.steps_finding()
        result = publish(self.reread())

        self.assertEqual(result["steps"], len(STORE_LEADS))
        self.assertEqual(steps.state, readiness.READY)
        self.assertEqual(steps.answer, f"{result['steps']} steps")


class PrecipitationHeldAtRunStartTests(PrecipitationStoreTestCase):
    """A source that does hold the run's start is read as provided."""

    precipitation_not_held = ()
    offset = 0.3

    def test_the_held_step_is_read_not_replaced_by_zero(self):
        """0.3 at the start and 1.8 at +6 h is a 1.5 window; a zero would make it 1.8."""
        self.close_run()

        publish(self.publication)

        self.assertAlmostEqual(self.first_value("precipitation_amount_acc6h"), 1.5, places=5)


class PrecipitationMissingMidRunTests(PrecipitationStoreTestCase):
    """Only the run's start is zero: a step missing anywhere else stays missing."""

    precipitation_not_held = (0, 2)

    def test_a_step_missing_after_the_start_is_not_published(self):
        self.close_run()

        planned = plan(self.reread())

        self.assertEqual(self.leads(planned.times), _hours(0, 6, 18, 24))

    def test_readiness_counts_the_zero_among_the_steps_the_build_writes(self):
        self.close_run()

        steps = self.steps_finding()
        result = publish(self.reread())

        self.assertEqual(result["steps"], 4)
        self.assertEqual(steps.answer, f"4 of {len(STORE_LEADS)}")


class InstantSlotMissingRunStartTests(PrecipitationStoreTestCase):
    """The zero is precipitation's only: an instant slot lacking the run's start
    still drops the first window through the shared-time intersection."""

    temperature_not_held = (0,)

    def test_the_first_window_is_dropped(self):
        self.close_run()

        planned = plan(self.reread())
        publish(self.reread())

        self.assertEqual(self.leads(planned.times), _hours(6, 12, 18, 24))
        self.assertEqual(self.window_ends("precipitation_amount_acc6h"), _hours(12, 18, 24))


class ZeroInTheAccumulationCheckTests(PrecipitationStoreTestCase):
    """The check that precipitation really accumulates runs over the zero too:
    this series rises from step to step, but falls from the zero before it."""

    offset = -3.0
    per_step = 0.1

    def test_a_series_falling_from_the_zero_is_refused(self):
        self.close_run()

        with self.assertRaisesRegex(ValueError, "not accumulating"):
            publish(self.publication)
