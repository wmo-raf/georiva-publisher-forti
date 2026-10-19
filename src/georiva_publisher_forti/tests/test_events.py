"""``PublicationWritten``: which run, at which revision, the app is showing.

A publish is the last link of a chain that started with an arrival — a fetch,
an upload, a file landing in the bucket — and ran through the ingestion of every
file of a model run to the run closing. These tests drive that last link the way
production does: the run closes, core hands the receiver its
``RunIngestionClosed``, the receiver dispatches, and the task runs on the far side
of a queue with only what the task headers carried.

The queue hop is the part that is easy to fake by accident. ``delay`` is patched
to catch what *would* have been published — the arguments, and the context the
publish signal stamps onto the headers — and the task is then run with exactly
those headers. A test that ran the task inline inside the dispatcher's context
would pass whether or not anything survived the queue.
"""

import uuid
from unittest.mock import patch

from django.db import DatabaseError, connection, transaction
from django.test import TestCase
from django.urls import reverse

from georiva.events.context import current_context
from georiva.events.recording import record
from georiva.events.tasks import ChainedTask, EntryTask, SweepTask, context_to_headers
from georiva.events.testing import event_context
from georiva.ingestion.models import RunIngestion, RunIngestionClosed
from georiva.organisations.testing import dial_org
from georiva_publisher_forti import tasks
from georiva_publisher_forti.models import ACTOR, SINK_ROOT, FortiPublication, PublicationWritten
from georiva_publisher_forti.publisher import publish

from .factories import REFERENCE_TIME, make_collection, make_org_admin, make_publication
from .test_publisher import PublishTestCase

SWEEP_ACTOR = "sweep:sweep_forti_publications"


class DispatchCapture:
    """Stands in for ``publish_forti_area.delay`` and remembers what went on the queue."""

    def __init__(self):
        self.dispatched = []

    def __call__(self, *args):
        self.dispatched.append((args, current_context()))

    def run_all(self):
        """Run every dispatched publish with the headers its dispatch carried."""
        for args, context in self.dispatched:
            headers = context_to_headers(context) if context is not None else {}
            tasks.publish_forti_area.apply(args=args, headers=headers).get()


class ChainTestCase(PublishTestCase):
    def setUp(self):
        super().setUp()
        self.run = RunIngestion.objects.get(collection=self.collection)
        self.organisation = self.publication.organisation

    def dispatching(self):
        capture = DispatchCapture()
        self.enterContext(patch.object(tasks.publish_forti_area, "delay", side_effect=capture))
        return capture

    def reopen_and_close(self):
        """A late file reopens the run and it closes again, as the ingestion task would.

        Returns the ``RunIngestionClosed`` the close wrote — the event the
        receiver is handed.
        """
        with self.captureOnCommitCallbacks(execute=True):
            RunIngestion.record_file(self.collection, REFERENCE_TIME)
        with (
            self.captureOnCommitCallbacks(execute=True),
            event_context(actor="celery", organisation_id=self.organisation.pk),
        ):
            RunIngestion.objects.get(pk=self.run.pk).close(RunIngestion.Closer.DECLARED_SET)
        return RunIngestionClosed.objects.filter(run=self.run).order_by("-occurred_at").first()


class RunClosedPublishTests(ChainTestCase):
    def test_a_closed_run_s_publish_is_recorded_in_the_run_s_chain(self):
        capture = self.dispatching()

        closed = self.reopen_and_close()
        capture.run_all()

        self.assertEqual(self.reread().status, FortiPublication.Status.READY)
        written = PublicationWritten.objects.get()
        self.assertEqual(written.correlation_id, closed.correlation_id)
        self.assertEqual(written.caused_by_id, closed.pk)
        self.assertEqual(written.organisation_id, self.organisation.pk)
        self.assertEqual(written.actor, ACTOR)

    def test_the_event_names_the_run_the_revision_and_the_version_written(self):
        capture = self.dispatching()

        closed = self.reopen_and_close()
        capture.run_all()

        written = PublicationWritten.objects.get()
        publication = self.reread()
        self.assertEqual(written.publication_id, publication.pk)
        self.assertEqual(written.publication_slug, publication.slug)
        self.assertEqual(written.area_key, publication.area_key)
        self.assertEqual(written.root, SINK_ROOT)
        self.assertEqual(written.run_id, self.run.pk)
        self.assertEqual(written.collection_slug, self.collection.slug)
        self.assertEqual(written.catalog_slug, self.collection.catalog.slug)
        self.assertEqual(written.reference_time, REFERENCE_TIME)
        self.assertEqual(written.revision, closed.revision)
        self.assertEqual(written.version, publication.published_version)

    def test_the_event_counts_what_the_publish_wrote(self):
        capture = self.dispatching()

        self.reopen_and_close()
        capture.run_all()

        written = PublicationWritten.objects.get()
        self.assertEqual(written.point_count, 25)
        self.assertEqual(written.step_count, 9)
        self.assertEqual(written.parameter_count, len(self.reread().published_parameters))
        self.assertGreater(written.objects_written, 0)
        self.assertEqual(written.versions_pruned, 0)

    def test_a_reopened_run_records_no_publication(self):
        """A reopen only marks stale: more of the run is still arriving."""
        capture = self.dispatching()

        with self.captureOnCommitCallbacks(execute=True):
            RunIngestion.record_file(self.collection, REFERENCE_TIME)
        capture.run_all()

        self.assertEqual(capture.dispatched, [])
        self.assertFalse(PublicationWritten.objects.exists())


class SweptPublishTests(ChainTestCase):
    def make_stale(self):
        FortiPublication.objects.filter(pk=self.publication.pk).update(status=FortiPublication.Status.STALE)

    def test_a_swept_publish_keeps_the_publication_s_chain_under_the_sweep_s_actor(self):
        earlier_chain = uuid.uuid4()
        with event_context(organisation_id=self.organisation.pk, correlation_id=earlier_chain):
            earlier = record(PublicationWritten, **_snapshot(self.publication))
        self.make_stale()
        capture = self.dispatching()

        tasks.sweep_forti_publications()
        capture.run_all()

        written = PublicationWritten.objects.exclude(pk=earlier.pk).get()
        self.assertEqual(written.correlation_id, earlier_chain)
        self.assertEqual(written.actor, SWEEP_ACTOR)
        self.assertEqual(written.organisation_id, self.organisation.pk)
        self.assertIsNone(written.caused_by_id)

    def test_a_publication_never_written_starts_its_own_chain_in_its_own_organisation(self):
        self.make_stale()
        capture = self.dispatching()

        tasks.sweep_forti_publications()
        capture.run_all()

        written = PublicationWritten.objects.get()
        self.assertEqual(written.actor, SWEEP_ACTOR)
        self.assertEqual(written.organisation_id, self.organisation.pk)
        self.assertIsNone(written.caused_by_id)

    def test_each_publication_is_swept_into_its_own_chain(self):
        """One sweep pass, two organisations: a chain is single-organisation."""
        other = make_publication(make_collection(org_slug="other-org"))
        for publication, chain in ((self.publication, uuid.uuid4()), (other, uuid.uuid4())):
            with event_context(organisation_id=publication.organisation.pk, correlation_id=chain):
                record(PublicationWritten, **_snapshot(publication))
        FortiPublication.objects.update(status=FortiPublication.Status.STALE)
        capture = self.dispatching()

        tasks.sweep_forti_publications()

        chains = {args[0]: context for args, context in capture.dispatched}
        for publication in (self.publication, other):
            last = PublicationWritten.objects.get(publication=publication)
            with self.subTest(publication=publication.area_key):
                self.assertEqual(chains[publication.pk].correlation_id, last.correlation_id)
                self.assertEqual(chains[publication.pk].organisation_id, publication.organisation.pk)


class PublishRecordingTests(ChainTestCase):
    def test_a_publish_with_nothing_new_records_nothing(self):
        with event_context(organisation_id=self.organisation.pk):
            publish(self.publication)
            publish(self.reread())

        self.assertEqual(PublicationWritten.objects.count(), 1)

    def test_a_publish_run_by_hand_starts_a_chain_of_its_own(self):
        """A shell or a management command has no context; the publish is still a fact."""
        publish(self.publication)

        written = PublicationWritten.objects.get()
        self.assertEqual(written.actor, ACTOR)
        self.assertEqual(written.organisation_id, self.organisation.pk)

    def test_the_event_table_rejects_raw_sql_update_and_delete(self):
        with event_context(organisation_id=self.organisation.pk):
            publish(self.publication)
        table = PublicationWritten._meta.db_table

        for sql in (f"UPDATE {table} SET revision = 99", f"DELETE FROM {table}"):
            with self.subTest(sql=sql), self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql)

    def test_deleting_the_publication_leaves_its_history(self):
        with event_context(organisation_id=self.organisation.pk):
            publish(self.publication)

        self.publication.delete()

        written = PublicationWritten.objects.get()
        self.assertIsNone(written.publication_id)
        self.assertIsNone(written.subject_url())


class TimelineTests(ChainTestCase):
    def publish_from_a_close(self):
        capture = self.dispatching()
        closed = self.reopen_and_close()
        capture.run_all()
        return closed

    def test_the_publication_appears_in_the_run_s_timeline_caused_by_its_close(self):
        closed = self.publish_from_a_close()
        self.client.force_login(make_org_admin("amina"))
        dial_org(self.client, self.organisation.slug)

        response = self.client.get(reverse("events_timeline", args=[closed.correlation_id]))

        rows = {row["label"]: row for row in response.context["rows"]}
        self.assertIn(PublicationWritten.label, rows)
        self.assertEqual(rows[PublicationWritten.label]["cause"], f"{closed.label}: {closed.summary()}")

    def test_another_organisation_does_not_see_it(self):
        closed = self.publish_from_a_close()
        make_collection(org_slug="other-org")
        self.client.force_login(make_org_admin("outsider", org_slug="other-org"))
        dial_org(self.client, "other-org")

        response = self.client.get(reverse("events_timeline", args=[closed.correlation_id]))

        self.assertEqual(response.status_code, 404)


class TaskBaseTests(TestCase):
    """A plainly declared task keeps working but drops the chain."""

    def test_the_publish_continues_the_chain_it_was_dispatched_in(self):
        self.assertIsInstance(tasks.publish_forti_area, ChainedTask)

    def test_the_sweep_is_a_sweep(self):
        self.assertIsInstance(tasks.sweep_forti_publications, SweepTask)

    def test_the_scheduled_passes_start_their_own_chains(self):
        for task in (tasks.refresh_forti_config, tasks.prune_forti_publications):
            with self.subTest(task=task.name):
                self.assertIsInstance(task, EntryTask)


def _snapshot(publication):
    """The fields a ``PublicationWritten`` needs, for an earlier publish recorded by hand."""
    collection = publication.collection
    return {
        "publication": publication,
        "publication_slug": publication.slug,
        "area_key": publication.area_key,
        "root": SINK_ROOT,
        "collection_slug": collection.slug,
        "catalog_slug": collection.catalog.slug,
        "reference_time": REFERENCE_TIME,
        "revision": 1,
        "version": 1,
        "point_count": 25,
        "step_count": 9,
        "parameter_count": 8,
        "objects_written": 10,
        "versions_pruned": 0,
    }
