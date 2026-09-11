"""Both config documents, and the rules that decide whether they are written.

Two of those rules are only visible from the reader's side, and both fail late:
a document either process would reject is kept out of the previous config by
``configwatch`` while the process runs, and reaches ``log.Fatalf`` the next time
it starts. So the test for each is that the bad document is *not written*, not
that it is written and rejected.
"""

import json
from unittest.mock import patch

from django.test import TestCase

from georiva_publisher_forti import config, rdfconfig

from .factories import make_collection, make_publication
from .sink_isolation import TemporarySinkMixin

PUBLISHED = 178835040000

STORAGE = {
    "bucket": "georiva-publications",
    "endpoint": "http://georiva-minio:9000",
    "region": "us-east-1",
}


class DocumentTests(TestCase):
    def setUp(self):
        self.publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["air_temperature_2m", "precipitation_amount_acc6h"],
        )

    def documents(self, publications=None):
        return config.documents(
            [self.publication] if publications is None else publications,
            **STORAGE,
        )

    def test_both_documents_come_out_of_one_call(self):
        """Two projections of one question — what is this instance serving — so
        building them apart is how they come to disagree."""
        self.assertEqual(
            sorted(self.documents()),
            sorted([config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH]),
        )

    def test_both_live_under_the_config_directory(self):
        """D21 makes ``_forti/config/`` the sidecar's one source. §6's diagram
        still shows jsonformat.json at the root of the prefix; the diagram
        predates D21, and a document outside the directory is a second thing to
        remember to copy."""
        for path in (config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH):
            with self.subTest(path=path):
                self.assertTrue(path.startswith(f"{config.CONFIG_PREFIX}/"))

    def test_the_area_list_is_rooted_at_the_shared_prefix(self):
        """The reader's ``?prefix=`` is the sink root and nothing narrower."""
        from georiva_publisher_forti.models import SINK_ROOT

        bucket = self.documents()[config.RAWDATAFORECASTER_PATH]["source"]["bucket"]

        self.assertIn(f"prefix={SINK_ROOT}/", bucket)

    def test_a_disabled_publication_reaches_neither_document(self):
        self.publication.is_enabled = False

        self.assertEqual(self.documents(), {})

    def test_the_distance_comes_from_this_plugin_not_from_core(self):
        """D19: core knows nothing about Forti, so the one forti-shaped number
        has its default here and an override read here."""
        document = config.documents([self.publication], **STORAGE, maximum_gridpoint_distance=25000)

        self.assertEqual(document[config.RAWDATAFORECASTER_PATH]["maximum_gridpoint_distance"], 25000)
        self.assertEqual(
            self.documents()[config.RAWDATAFORECASTER_PATH]["maximum_gridpoint_distance"],
            rdfconfig.DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE,
        )


class RefusalTests(TestCase):
    """A document the reader is guaranteed to reject is worse than no document.

    Both processes validate the same way and fail the same way: ``configwatch``
    keeps the previous config when handed a bad one, so a *running* process
    shrugs it off, while ``watcher.Load()`` at startup takes the same rejection
    to ``log.Fatalf``. The damage is invisible until the next restart.
    """

    def documents(self, publications):
        return config.documents(publications, **STORAGE)

    def test_a_jsonformat_with_no_parameters_is_not_written(self):
        """``Problems()`` (`config.go:75`) makes an empty parameters map fatal.

        It happens whenever no enabled publication has published a parameter yet:
        a freshly created area, or the last published one disabled. The right
        document in every such case is the one already there.
        """
        never_published = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=None,
            published_parameters=[],
        )

        self.assertNotIn(config.JSONFORMAT_PATH, self.documents([never_published]))

    def test_a_rawdataforecaster_with_no_areas_is_not_written(self):
        """``Problems()`` (`config/reload.go`) says as much in words: "none
        configured, so every request would answer no datasets available"."""
        never_published = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=None,
            published_parameters=["air_temperature_2m"],
        )

        self.assertNotIn(config.RAWDATAFORECASTER_PATH, self.documents([never_published]))

    def test_a_published_area_with_a_retired_parameter_set_keeps_its_area_list(self):
        """The two refusals are independent. An area whose only parameter this
        plugin no longer knows leaves jsonformat empty, and taking the area list
        down with it would be one unrelated document deciding another."""
        publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["some_retired_parameter"],
        )

        documents = self.documents([publication])

        self.assertNotIn(config.JSONFORMAT_PATH, documents)
        self.assertIn(config.RAWDATAFORECASTER_PATH, documents)


class RefreshTests(TemporarySinkMixin, TestCase):
    def setUp(self):
        self.isolate_sink()
        self.publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["air_temperature_2m"],
        )

    def read(self, path):
        from georiva_publisher_forti.models import instance_sink

        return json.loads(instance_sink().read_bytes(path).decode("utf-8"))

    def test_the_first_refresh_writes_both_documents(self):
        written = config.refresh()

        self.assertEqual(sorted(written), sorted([config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH]))
        self.assertEqual(self.read(config.RAWDATAFORECASTER_PATH)["areas"], [self.publication.area_key])

    def test_an_unchanged_document_is_not_rewritten(self):
        """Not an optimisation. The sidecar polls these files and cannot tell a
        new ``LastModified`` from a new document, so an unconditional write turns
        every five-minute tick into a config reload on both processes."""
        config.refresh()

        self.assertEqual(config.refresh(), [])

    def test_a_changed_document_is_rewritten_and_the_other_is_left_alone(self):
        """One query, two documents, and only the one that moved goes up."""
        config.refresh()

        second = make_publication(
            make_collection(slug="aifs-surface"),
            slug="ecmwf-aifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            # The same parameters, so jsonformat.json is byte-identical.
            published_parameters=["air_temperature_2m"],
        )

        self.assertEqual(config.refresh(), [config.RAWDATAFORECASTER_PATH])
        self.assertEqual(
            self.read(config.RAWDATAFORECASTER_PATH)["areas"],
            sorted([self.publication.area_key, second.area_key]),
        )

    def test_an_unreadable_document_is_rewritten_rather_than_assumed_current(self):
        """The only use of the comparison is "may I skip the write", and the safe
        answer to a question that could not be asked is no."""
        config.refresh()

        with patch.object(config, "_current_sha", return_value=None):
            self.assertEqual(
                sorted(config.refresh()),
                sorted([config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH]),
            )

    def test_the_documents_are_the_union_over_every_organisation(self):
        """One file governs every tenant's serving (D20/D21), so a per-org writer
        would leave whichever organisation refreshed last as the only one whose
        areas survive."""
        other = make_publication(
            make_collection(org_slug="other-org"),
            slug="gfs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["precipitation_amount_acc6h"],
        )

        config.refresh()

        self.assertNotEqual(self.publication.organisation, other.organisation)
        self.assertEqual(
            self.read(config.RAWDATAFORECASTER_PATH)["areas"],
            sorted([self.publication.area_key, other.area_key]),
        )
        self.assertEqual(
            sorted(self.read(config.JSONFORMAT_PATH)["parameters"]),
            ["instant", "next_6_hours"],
        )

    def test_the_config_is_not_a_completion_marker(self):
        """``write`` refuses marker paths outright, and these are not markers:
        nothing loads on their appearance, and ``latest/<area key>`` is what makes
        a version real."""
        from georiva_publisher_forti.models import instance_sink

        sink = instance_sink()

        for path in (config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH):
            with self.subTest(path=path):
                self.assertFalse(sink.is_marker(path))


class EncodingTests(TestCase):
    def test_the_same_document_encodes_to_the_same_bytes(self):
        """The sha comparison is only worth anything if it is."""
        document = {"b": 1, "a": [2, 3]}

        self.assertEqual(config.encode(document), config.encode({"a": [2, 3], "b": 1}))

    def test_the_bytes_are_utf8_json_a_go_process_can_read(self):
        self.assertEqual(json.loads(config.encode({"a": "ê"}).decode("utf-8")), {"a": "ê"})
