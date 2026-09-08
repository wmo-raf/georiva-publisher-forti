"""Where a publish runs, and how it is put there.

ADR 0025's rule is the one thing a plugin can break for the whole instance:
``georiva-ingestion`` runs one pool process, so anything queued there delays
every file behind it. A publish is derived from data already published and
already servable — nothing a reader can observe waits on it — so it belongs on
``georiva-processing``, and its sweeps on ``georiva-default``.

The second half of the rule is that a task's declared queue is the only routing
there is: ``apply_async(queue=...)`` silently wins over the declaration, which is
how two core tasks stayed on the wrong queue for months.
"""

import ast
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from georiva_publisher_forti import tasks
from georiva_publisher_forti.models import FortiPublication

from .factories import make_collection, make_publication

PACKAGE_ROOT = Path(tasks.__file__).resolve().parent


class QueueRoutingTests(SimpleTestCase):
    def test_the_publish_is_deferrable_and_runs_on_the_processing_queue(self):
        self.assertEqual(tasks.publish_forti_area.queue, "georiva-processing")

    def test_the_sweeps_run_on_the_default_queue(self):
        for task in (
            tasks.sweep_forti_publications,
            tasks.refresh_forti_jsonformat,
            tasks.prune_forti_publications,
        ):
            with self.subTest(task=task.name):
                self.assertEqual(task.queue, "georiva-default")

    def test_nothing_this_plugin_declares_lands_on_the_ingestion_queue(self):
        queues = {
            task.queue
            for task in (
                tasks.publish_forti_area,
                tasks.sweep_forti_publications,
                tasks.refresh_forti_jsonformat,
                tasks.prune_forti_publications,
            )
        }

        self.assertNotIn("georiva-ingestion", queues)

    def test_no_dispatch_site_overrides_the_queue_its_task_declares(self):
        """The same AST sweep core runs over itself, over this package."""
        overrides = []
        for path in PACKAGE_ROOT.rglob("*.py"):
            if "tests" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "apply_async"
                    and any(keyword.arg == "queue" for keyword in node.keywords)
                ):
                    overrides.append((path.name, node.lineno))

        self.assertEqual(overrides, [])


class DispatchTests(TestCase):
    def setUp(self):
        self.publication = make_publication(make_collection())

    def test_dispatch_claims_the_publication_before_queueing_it(self):
        with patch.object(tasks.publish_forti_area, "delay") as delay:
            self.assertTrue(tasks.dispatch_publish(self.publication.pk))

        publication = FortiPublication.objects.get(pk=self.publication.pk)
        self.assertEqual(publication.status, FortiPublication.Status.BUILDING)
        delay.assert_called_once_with(self.publication.pk, publication.locked_by)

    def test_a_publication_already_in_flight_is_not_queued_again(self):
        with patch.object(tasks.publish_forti_area, "delay"):
            tasks.dispatch_publish(self.publication.pk)

            self.assertFalse(tasks.dispatch_publish(self.publication.pk))

    def test_a_disabled_publication_is_left_out_of_the_sweep(self):
        """A disabled publication left PENDING would otherwise be claimed and
        dispatched every five minutes, forever."""
        self.publication.is_enabled = False
        self.publication.save(update_fields=["is_enabled"])

        self.assertNotIn(
            self.publication.pk,
            list(FortiPublication.get_buildable().values_list("pk", flat=True)),
        )

    def test_the_sweep_dispatches_a_stale_publication(self):
        self.publication.mark_ready()
        FortiPublication.objects.filter(pk=self.publication.pk).update(status=FortiPublication.Status.STALE)

        with patch.object(tasks.publish_forti_area, "delay") as delay:
            tasks.sweep_forti_publications()

        delay.assert_called_once()


class StandDownTests(TestCase):
    def test_a_copy_whose_claim_was_recycled_does_not_publish(self):
        """A queue wait longer than LOCK_TIMEOUT lets the sweep free the row and
        dispatch a replacement. Two publishes writing one area is exactly the
        race the claim exists to prevent."""
        publication = make_publication(make_collection())
        FortiPublication.objects.filter(pk=publication.pk).update(
            status=FortiPublication.Status.BUILDING, locked_by="claim-2"
        )

        with patch("georiva_publisher_forti.publisher.publish") as published:
            tasks.publish_forti_area.run(publication.pk, "claim-1")

        published.assert_not_called()


class RunSignalTests(TestCase):
    def test_a_closed_run_dispatches_a_publish(self):
        """RunIngestion is the right granularity. The alternative — Asset.post_save
        — fires ~950 times per ECMWF run to produce one publish, and never once
        says the inputs are whole."""
        from georiva.ingestion.models import RunIngestion

        from .factories import REFERENCE_TIME

        publication = make_publication(make_collection())
        run = RunIngestion.objects.create(collection=publication.collection, reference_time=REFERENCE_TIME)

        with patch.object(tasks.publish_forti_area, "delay") as delay:
            run.close(RunIngestion.Closer.DECLARED_SET)

        delay.assert_called_once()

    def test_a_reopened_run_marks_stale_without_publishing(self):
        """A reopen means more files are still arriving; publishing now burns a
        version the next arrival immediately supersedes."""
        from georiva.ingestion.models import RunIngestion

        from .factories import REFERENCE_TIME

        publication = make_publication(make_collection())
        run = RunIngestion.objects.create(collection=publication.collection, reference_time=REFERENCE_TIME)
        with patch.object(tasks.publish_forti_area, "delay"):
            run.close(RunIngestion.Closer.DECLARED_SET)

        # The build that close dispatched has finished.
        FortiPublication.objects.get(pk=publication.pk).mark_ready()

        with patch.object(tasks.publish_forti_area, "delay") as delay:
            RunIngestion.record_file(publication.collection, REFERENCE_TIME)

            delay.assert_not_called()

        self.assertEqual(
            FortiPublication.objects.get(pk=publication.pk).status,
            FortiPublication.Status.STALE,
        )

    def test_a_reopen_arriving_mid_build_does_not_disturb_the_running_build(self):
        """mark_stale deliberately does not touch a BUILDING row — dropping a
        live claim is how two publishes end up writing one area. The reopen is
        picked up when the run closes again, which is what a reopened run does."""
        from georiva.ingestion.models import RunIngestion

        from .factories import REFERENCE_TIME

        publication = make_publication(make_collection())
        run = RunIngestion.objects.create(collection=publication.collection, reference_time=REFERENCE_TIME)
        with patch.object(tasks.publish_forti_area, "delay"):
            run.close(RunIngestion.Closer.DECLARED_SET)

        in_flight = FortiPublication.objects.get(pk=publication.pk)
        self.assertEqual(in_flight.status, FortiPublication.Status.BUILDING)
        claim = in_flight.locked_by

        RunIngestion.record_file(publication.collection, REFERENCE_TIME)

        after = FortiPublication.objects.get(pk=publication.pk)
        self.assertEqual(after.status, FortiPublication.Status.BUILDING)
        self.assertEqual(after.locked_by, claim)
