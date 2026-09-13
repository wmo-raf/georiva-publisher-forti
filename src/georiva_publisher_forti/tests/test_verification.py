"""The four hops, and the differences between them that a panel could flatten.

Every test here is a *distinction* rather than a feature. The panel's whole
value is telling apart states that look alike from Django's side, and all four
of the ones that matter are true of this instance today:

- nothing is published under ``_forti/`` — so "no file yet" is the ordinary
  state, and must not read as a failure;
- MinIO is up — so "could not read" is the state that would be developed
  blind, and is the one that must not read as "no file yet";
- the pair has never run here — so a document that *arrived and was refused*
  looks exactly like one that loaded, unless somebody checks ``ok``;
- and no area is resident — so ``null`` and ``0`` have never had to be told
  apart, which is the whole of M5.1's first fix.
"""

import json
import threading
import time
from unittest.mock import patch

from django.test import TestCase

from georiva_publisher_forti import config, verification
from georiva_publisher_forti.config import ABSENT, PRESENT, UNREACHABLE
from georiva_publisher_forti.verification import FOREIGN, REFUSED, WITHHELD

from .factories import make_collection, make_publication
from .sink_isolation import TemporarySinkMixin

PUBLISHED = 178835040000
LATER = 178856640000

RAWDATAFORECASTER = "rawdataforecaster.json"
JSONFORMAT = "jsonformat.json"


class PanelTestCase(TemporarySinkMixin, TestCase):
    """One publication, an isolated sink, and helpers to put each hop where it
    would be. Nothing here reaches an HTTP layer: every state worth asserting is
    a state of the bucket."""

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
        self.area = self.publication.area_key

    def sink(self):
        from georiva_publisher_forti.models import instance_sink

        return instance_sink()

    def intended_sha(self, path):
        return config.intended()[path]

    def write_status(self, path, payload):
        self.sink().write(path, json.dumps(payload).encode("utf-8"))

    def write_sidecar(self, volume, **overrides):
        payload = {
            "module": "sidecar",
            "compose_version": "0.0.1",
            "checked_at": "2026-09-13T09:00:00Z",
            "interval_seconds": 60,
            "fetch_ok": True,
            "upload_ok": True,
            "volume": volume,
        }
        payload.update(overrides)
        self.write_status(verification.SIDECAR_PATH, payload)

    def write_forecaster(self, loaded_sha, *, areas=None, store_error=None, ok=True, errors=None):
        state = {"areas": areas if areas is not None else []}
        if store_error:
            state["store_error"] = store_error
        payload = {
            "module": "rawdataforecaster",
            "loaded_sha": loaded_sha,
            "loaded_at": "2026-09-13T09:00:10Z",
            "ok": ok,
            "state": state,
        }
        if errors:
            payload["errors"] = errors
        self.write_status(verification.RAWDATAFORECASTER_STATUS_PATH, payload)

    def chain(self, report, document=RAWDATAFORECASTER):
        return next(chain for chain in report.chains if chain.document == document)

    def hop(self, report, name, document=RAWDATAFORECASTER):
        return next(hop for hop in self.chain(report, document).hops if hop.name == name)


class NotCutOverTests(PanelTestCase):
    """The state this instance is in right now, and the one an operator is most
    likely to meet first. It has to render as four things that have not happened
    yet, not as four failures."""

    def test_nothing_published_is_absent_at_every_hop_past_the_first(self):
        report = verification.report(sink=self.sink())

        for name in ("bucket", "volume", "loaded"):
            with self.subTest(hop=name):
                self.assertEqual(self.hop(report, name).presence, ABSENT)

    def test_a_hop_that_has_not_happened_is_not_a_disagreement(self):
        """``agrees`` is three-valued. ``None`` is "nothing to compare" and
        ``False`` is "compared, and different"; collapsing them is how a panel
        reports a fresh instance as a broken one."""
        report = verification.report(sink=self.sink())

        for name in ("bucket", "volume", "loaded"):
            with self.subTest(hop=name):
                self.assertIsNone(self.hop(report, name).agrees)

    def test_the_chain_names_the_first_hop_that_has_not_happened(self):
        report = verification.report(sink=self.sink())

        self.assertEqual(self.chain(report).broken_at, "bucket")

    def test_the_areas_table_says_the_reader_has_no_state_rather_than_no_areas(self):
        report = verification.report(sink=self.sink())

        self.assertEqual(report.areas, ())
        self.assertIn("never written a status file", report.areas_detail)


class OutageTests(PanelTestCase):
    """An object store that will not answer is not an object store with nothing
    on it. Both are "no sha", and only one of them is somebody's afternoon."""

    def test_a_failing_read_is_unreachable_not_absent(self):
        sink = self.sink()

        with patch.object(type(sink), "exists", side_effect=OSError("connection refused")):
            report = verification.report(sink=sink)

        self.assertEqual(self.hop(report, "bucket").presence, UNREACHABLE)
        self.assertIn("connection refused", self.hop(report, "bucket").detail)

    def slow_report(self, *, blocking_for=3.0, deadline=0.05):
        """A report over a bucket that will not answer within the deadline.

        The delay is in the fake rather than in a zero deadline on the real
        reads: a filesystem-backed sink answers in microseconds, so
        ``deadline=0`` is a race between the worker finishing and the caller
        starting to wait — and a flaky test of a timeout is worse than none.
        """
        started = threading.Event()

        def slow(sink):
            started.set()
            time.sleep(blocking_for)

        with patch.object(verification, "_read_all", slow):
            report = verification.report(sink=self.sink(), deadline=deadline)
        return report, started

    def test_a_bucket_that_does_not_answer_in_time_renders_rather_than_blocks(self):
        """The reads run in one worker thread under one deadline. A page that
        says it could not ask beats a page that holds an admin worker through
        botocore's retry ladder."""
        report, _ = self.slow_report()

        self.assertFalse(report.answered)
        self.assertEqual(self.hop(report, "bucket").presence, UNREACHABLE)
        self.assertIn("did not answer", self.hop(report, "bucket").detail)

    def test_the_deadline_does_not_wait_for_the_thread_it_abandoned(self):
        """``shutdown(wait=False)`` is the point: a socket blocked in recv does
        not care that nobody is waiting, so the request must not either. The
        assertion is on the clock — the call returns while the read is still
        running, which ``shutdown(wait=True)`` would not allow."""
        began = time.monotonic()
        report, started = self.slow_report(blocking_for=5.0, deadline=0.1)
        elapsed = time.monotonic() - began

        self.assertTrue(started.is_set())
        self.assertFalse(report.answered)
        self.assertLess(elapsed, 2.0)


class WithheldTests(PanelTestCase):
    """A document that would be rejected is absent from ``intended``, and absent
    means "leave what is on the bucket alone" — never "delete it", and never a
    mismatch."""

    def setUp(self):
        super().setUp()
        self.publication.published_parameters = []
        self.publication.save()

    def test_a_document_that_would_be_rejected_is_withheld_not_missing(self):
        report = verification.report(sink=self.sink())

        self.assertEqual(self.hop(report, "intended", JSONFORMAT).presence, WITHHELD)

    def test_a_withheld_document_with_a_pair_still_running_is_not_a_fault(self):
        """An empty ``parameters`` map is fatal to jsonfrontend, so the right
        document is the one already there. The panel has to say that rather than
        render three disagreeing hops."""
        sha = "a" * 64
        self.sink().write(config.JSONFORMAT_PATH, b"{}")
        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: sha})
        self.write_status(
            verification.JSONFRONTEND_STATUS_PATH,
            {"module": "jsonfrontend", "loaded_sha": sha, "loaded_at": "2026-09-13T09:00:00Z", "ok": True},
        )

        chain = self.chain(verification.report(sink=self.sink()), JSONFORMAT)

        self.assertIsNone(chain.broken_at)
        self.assertIn("correct outcome and not a stale one", chain.verdict)


class AgreementTests(PanelTestCase):
    """What "compared by sha" means once all four hops exist."""

    def setUp(self):
        super().setUp()
        config.refresh()
        self.sha = self.intended_sha(config.RAWDATAFORECASTER_PATH)

    def test_a_document_that_reached_every_hop_agrees_at_every_hop(self):
        self.write_sidecar({RAWDATAFORECASTER: self.sha, JSONFORMAT: None})
        self.write_forecaster(self.sha, areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])

        chain = self.chain(verification.report(sink=self.sink()))

        self.assertIsNone(chain.broken_at)
        self.assertTrue(all(hop.agrees for hop in chain.hops[1:]))

    def test_agreement_is_against_the_first_hop_so_the_chain_says_where_it_broke(self):
        """Compared pairwise, one stale hop makes every hop after it disagree
        with its neighbour and the join is left to be counted out by eye."""
        stale = "b" * 64
        self.write_sidecar({RAWDATAFORECASTER: stale, JSONFORMAT: None})
        self.write_forecaster(stale)

        chain = self.chain(verification.report(sink=self.sink()))

        self.assertEqual(chain.broken_at, "volume")
        self.assertTrue(chain.hops[1].agrees)
        self.assertFalse(chain.hops[2].agrees)
        self.assertFalse(chain.hops[3].agrees)

    def test_a_volume_a_document_behind_is_told_apart_from_a_volume_with_no_document(self):
        """The distinction the plan asks for in as many words: hop 3 disagreeing
        with hop 2, against hop 3 not having happened."""
        self.write_sidecar({RAWDATAFORECASTER: "c" * 64, JSONFORMAT: None})
        behind = self.hop(verification.report(sink=self.sink()), "volume")

        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: None})
        empty = self.hop(verification.report(sink=self.sink()), "volume")

        self.assertEqual((behind.presence, behind.agrees), (PRESENT, False))
        self.assertEqual((empty.presence, empty.agrees), (ABSENT, None))

    def test_a_refused_document_is_not_a_loaded_one_even_with_the_right_sha(self):
        """``loaded_sha`` is the digest of the bytes last *attempted*
        (`configwatch.go:256`), and a rejected document leaves the previous
        configuration running. Four matching shas would otherwise report a
        process serving something else as fully in agreement."""
        self.write_sidecar({RAWDATAFORECASTER: self.sha, JSONFORMAT: None})
        self.write_forecaster(self.sha, ok=False, errors=["areas: must not be empty"])

        chain = self.chain(verification.report(sink=self.sink()))

        self.assertEqual(chain.hops[3].presence, REFUSED)
        self.assertFalse(chain.hops[3].agrees)
        self.assertEqual(chain.broken_at, "loaded")
        self.assertIn("read this document and refused it", chain.verdict)


class StatusDocumentTests(PanelTestCase):
    """``status/rawdataforecaster.json`` and ``config/rawdataforecaster.json``
    share a basename and mean opposite things. This reader is the third place
    that could confuse them."""

    def test_a_status_document_under_the_wrong_name_is_refused_not_read(self):
        self.write_status(
            verification.JSONFRONTEND_STATUS_PATH,
            {"module": "rawdataforecaster", "loaded_sha": "d" * 64, "ok": True},
        )

        hop = self.hop(verification.report(sink=self.sink()), "loaded", JSONFORMAT)

        self.assertEqual(hop.presence, FOREIGN)
        self.assertIn("config/ and status/ share names", hop.detail)

    def test_a_sidecar_with_no_entry_for_a_document_is_a_different_compose_file(self):
        """The volume map always carries both documents — ``sha_of`` prints a
        literal ``null`` for a file that is not there — so a missing key is not
        a missing file."""
        self.write_sidecar({RAWDATAFORECASTER: "e" * 64})

        hop = self.hop(verification.report(sink=self.sink()), "volume", JSONFORMAT)

        self.assertEqual(hop.presence, FOREIGN)
        self.assertIn("different compose file", hop.detail)


class AreaTests(PanelTestCase):
    """What is actually resident, and at which version."""

    def setUp(self):
        super().setUp()
        config.refresh()
        self.sha = self.intended_sha(config.RAWDATAFORECASTER_PATH)
        self.write_sidecar({RAWDATAFORECASTER: self.sha, JSONFORMAT: None})

    def areas(self):
        return verification.report(sink=self.sink()).areas

    def test_null_is_not_zero(self):
        """M5.1's first fix, seen from the other end. ``available`` and
        ``loaded`` are ``*int`` with no ``omitempty``, so ``null`` is the answer
        and not the absence of one — and a Go map miss reading as version 0 is
        what made one unpublished area fatal for every organisation."""
        self.write_forecaster(self.sha, areas=[{"area": self.area, "available": None, "loaded": None}])

        row = self.areas()[0]

        self.assertIsNone(row.available)
        self.assertIsNone(row.loaded)
        self.assertFalse(row.ok)

    def test_a_resident_area_at_the_published_version_is_the_only_good_row(self):
        self.write_forecaster(self.sha, areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])

        row = self.areas()[0]

        self.assertTrue(row.ok)
        self.assertEqual(row.published, PUBLISHED)

    def test_a_newer_run_on_the_bucket_and_not_loaded_is_reported(self):
        self.write_forecaster(self.sha, areas=[{"area": self.area, "available": LATER, "loaded": PUBLISHED}])

        row = self.areas()[0]

        self.assertFalse(row.ok)
        self.assertIn("not loaded", row.verdict)

    def test_store_error_is_rendered_because_the_area_list_does_not_decay(self):
        """``available`` is the last listing that worked, so without the error a
        store nobody can reach is indistinguishable from one holding exactly
        what is loaded — which is the case the field exists for."""
        self.write_forecaster(
            self.sha,
            areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}],
            store_error="listing _forti/latest/: unparseable key",
        )

        self.assertIn("unparseable key", verification.report(sink=self.sink()).store_error)

    def test_an_area_georiva_intends_that_the_reader_does_not_list_gets_a_row(self):
        """Otherwise its absence shows only as a sha that disagrees, which does
        not name the area that is off the air."""
        self.write_forecaster(self.sha, areas=[])

        rows = self.areas()

        self.assertEqual([row.area for row in rows], [self.area])
        self.assertIn("behind the area list", rows[0].verdict)

    def test_an_image_without_the_status_fix_says_so(self):
        """A rawdataforecaster that publishes no state proves only that a
        *config* parsed, which is M5.1's second fix not being in the image."""
        self.write_status(
            verification.RAWDATAFORECASTER_STATUS_PATH,
            {"module": "rawdataforecaster", "loaded_sha": self.sha, "ok": True},
        )

        report = verification.report(sink=self.sink())

        self.assertEqual(report.areas, ())
        self.assertIn("M5.1's second fix", report.areas_detail)


class VersionTests(PanelTestCase):
    """The compose file against the installed plugin. ``test_compose.py`` makes
    the same comparison at build time; this makes it on the instance, because
    the compose file is fetched by hand and the plugin is installed through
    ``plugins.toml``."""

    def test_a_matching_pair_agrees(self):
        from importlib.metadata import version

        installed = version("georiva-publisher-forti")
        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: None}, compose_version=installed)

        self.assertIs(verification.report(sink=self.sink()).versions.agree, True)

    def test_a_drifted_pair_says_which_is_which(self):
        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: None}, compose_version="0.0.99")

        versions = verification.report(sink=self.sink()).versions

        self.assertIs(versions.agree, False)
        self.assertIn("0.0.99", versions.detail)

    def test_no_sidecar_is_nothing_to_compare_rather_than_a_mismatch(self):
        versions = verification.report(sink=self.sink()).versions

        self.assertIsNone(versions.agree)


class FreshnessTests(PanelTestCase):
    """Freshness is the timestamp inside the document, never the object's
    ``LastModified``: the sidecar's ``push_status`` uploads every pass whether or
    not anything moved, deliberately, and says so."""

    def test_the_age_comes_from_checked_at(self):
        from django.utils import timezone

        moment = timezone.now().isoformat().replace("+00:00", "Z")
        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: None}, checked_at=moment)

        report = verification.report(sink=self.sink())

        self.assertEqual(report.sidecar.checked_at, moment)
        self.assertIn("ago", report.sidecar.age)

    def test_a_timestamp_that_will_not_parse_leaves_the_raw_value_alone(self):
        """Go marshals RFC 3339 with nanosecond precision. A reader that cannot
        parse one must still show it rather than blank the row."""
        self.write_sidecar({RAWDATAFORECASTER: None, JSONFORMAT: None}, checked_at="not a time")

        report = verification.report(sink=self.sink())

        self.assertEqual(report.sidecar.checked_at, "not a time")
        self.assertEqual(report.sidecar.age, "")
