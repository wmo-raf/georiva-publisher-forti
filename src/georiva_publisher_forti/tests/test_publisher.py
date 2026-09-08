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
import shutil
import tempfile
from pathlib import Path

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings

from georiva.core.publishing import MarkerOrderingError, PublicationSink
from georiva.core.storage import Bucket, BucketType
from georiva_publisher_forti import publisher
from georiva_publisher_forti.models import MARKER_PATTERNS, SINK_SLUG, FortiPublication
from georiva_publisher_forti.publisher import GridMoved, publish

from .factories import make_collection, make_publication, make_run, write_cogs


class PublishTestCase(TestCase):
    def setUp(self):
        self.cogs = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cogs, ignore_errors=True)
        self.bucket_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bucket_root, ignore_errors=True)

        override = override_settings(
            STORAGES={
                **settings.STORAGES,
                "georiva-publications": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.bucket_root, "base_url": "/publications/"},
                },
            }
        )
        override.enable()
        self.addCleanup(override.disable)

        # The one seam between this plugin and object storage.
        self.bucket = Bucket(BucketType.PUBLICATIONS, "georiva-publications")
        self._patch_urls()
        self._patch_sink()

        self.collection = make_collection()
        self.publication = make_publication(self.collection)
        write_cogs(self.collection, self.cogs, step_hours=3, steps=9)
        make_run(self.collection)

    def _patch_urls(self):
        original = publisher.WindowReader
        cogs = self.cogs

        from georiva_publisher_forti import reader as reader_module

        original_asset_url = reader_module.asset_url
        reader_module.asset_url = lambda href: str(cogs / href)
        self.addCleanup(setattr, reader_module, "asset_url", original_asset_url)
        self.addCleanup(setattr, publisher, "WindowReader", original)

    def _patch_sink(self):
        bucket = self.bucket

        def sink(self_publication):
            return PublicationSink(
                self_publication.organisation.slug,
                SINK_SLUG,
                marker_patterns=MARKER_PATTERNS,
                bucket=bucket,
            )

        original = FortiPublication.sink
        FortiPublication.sink = sink
        self.addCleanup(setattr, FortiPublication, "sink", original)

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

        pointed_at = int(sink.read_bytes("latest/kenya").decode())

        self.assertEqual(pointed_at, result["version"])
        self.assertTrue(sink.exists(f"kenya/{pointed_at}/complete.json"))

    def test_the_tree_has_the_four_objects_a_reader_opens(self):
        publish(self.publication)
        sink = self.sink()
        grid_id = self.reread().grid_id

        for name in ("latitude", "longitude", "data", "meta.json"):
            with self.subTest(object=name):
                self.assertTrue(sink.exists(f"kenya/{self.reread().published_version}/{grid_id}/{name}"))

    def test_the_grid_directory_is_the_md5_of_the_coordinates(self):
        import hashlib

        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        latitude = sink.read_bytes(f"kenya/{version}/{grid_id}/latitude")
        longitude = sink.read_bytes(f"kenya/{version}/{grid_id}/longitude")

        self.assertEqual(hashlib.md5(latitude + longitude).hexdigest(), grid_id)

    def test_a_reader_finds_the_temperature_it_was_given(self):
        """The arithmetic ``values.Read`` does, against a known constant."""
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"kenya/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"kenya/{version}/{grid_id}/data"), dtype="<i2")

        entry = meta["parameters"]["air_temperature_2m"]
        stride = meta["number_of_points"]
        at_point_7_step_0 = values[7 * stride + entry["slice_from"]]

        self.assertAlmostEqual(float(at_point_7_step_0) * entry["scale_factor"], 24.0, places=5)

    def test_the_precipitation_window_is_what_fell_over_six_hours(self):
        """``tp`` accumulates by 0.5 mm per 3-hour step, so a 6-hour window is
        1.0 mm — and finding it needs two steps, not one."""
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"kenya/{version}/{grid_id}/meta.json"))
        values = np.frombuffer(sink.read_bytes(f"kenya/{version}/{grid_id}/data"), dtype="<i2")

        entry = meta["parameters"]["precipitation_amount_acc6h"]
        stride = meta["number_of_points"]
        first = values[0 * stride + entry["slice_from"]]

        self.assertAlmostEqual(float(first) * entry["scale_factor"], 1.0, places=5)

    def test_a_period_parameters_times_are_window_ends(self):
        publish(self.publication)
        sink = self.sink()
        version = self.reread().published_version
        grid_id = self.reread().grid_id

        meta = json.loads(sink.read_bytes(f"kenya/{version}/{grid_id}/meta.json"))
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


class MarkerOrderingTests(PublishTestCase):
    def test_no_marker_exists_while_the_bytes_are_still_going_down(self):
        """The property M0 proved a reader depends on. Checked by looking at the
        bucket at the moment the last ordinary object is written."""
        seen = {}
        sink = self.sink()
        original_write = type(sink).write

        def watched_write(self_sink, relpath, content):
            key = original_write(self_sink, relpath, content)
            seen[relpath] = self_sink.exists(f"latest/{self.publication.area}")
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

        self.assertFalse(self.sink().exists("latest/kenya"))

    def test_the_engine_refuses_markers_over_bytes_that_are_not_there(self):
        sink = self.sink()

        with self.assertRaises(MarkerOrderingError):
            from georiva_publisher_forti.writer import completion_markers

            sink.publish_markers(completion_markers("kenya", 1), require_staged=["kenya/1/grid/data"])


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
        self.assertEqual(int(self.sink().read_bytes("latest/kenya").decode()), second["version"])

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

    def test_only_the_newest_versions_survive(self):
        self._publish_versions(publisher.VERSIONS_KEPT + 2)

        remaining = self.sink().children("kenya")

        self.assertEqual(len(remaining), publisher.VERSIONS_KEPT)

    def test_a_pruned_version_takes_its_own_manifest_with_it(self):
        """``complete.json`` is a completion marker, and a version directory that
        cannot lose it never goes away."""
        versions = self._publish_versions(publisher.VERSIONS_KEPT + 1)
        sink = self.sink()

        self.assertFalse(sink.exists(f"kenya/{versions[0]}/complete.json"))

    def test_the_version_a_reader_is_following_is_never_pruned(self):
        versions = self._publish_versions(publisher.VERSIONS_KEPT + 1)
        sink = self.sink()

        current = int(sink.read_bytes("latest/kenya").decode())

        self.assertEqual(current, versions[-1])
        self.assertTrue(sink.exists(f"kenya/{current}/complete.json"))
