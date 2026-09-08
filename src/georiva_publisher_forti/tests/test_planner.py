"""What a publish would write, decided before anything is read.

The plan is built entirely from the database, so a run that cannot be published is
refused in milliseconds rather than after 70 MB of raster reads. Two of its
decisions are the ones that bite:

**The step intersection.** Variables of one run ingest independently and finish at
different step counts — 13 to 16 of 16 on a live run. A step some variables have
and others do not is a partial forecast, and Forti has no way to express one.

**The unit check.** Forti copies units out of ``meta.json`` without interpreting
them, so a collection retuned from celsius to kelvin publishes a number wrong by
273 under a label that says celsius, and nothing downstream can tell.
"""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from georiva.core.models import Collection, Unit
from georiva_publisher_forti.planner import NothingToPublish, PublicationRefused, plan

from .factories import REFERENCE_TIME, make_collection, make_publication, make_run, write_cogs


class PlannerTestCase(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.collection = make_collection()
        self.publication = make_publication(self.collection)


class RunSelectionTests(PlannerTestCase):
    def test_a_collection_with_no_closed_run_has_nothing_to_publish(self):
        write_cogs(self.collection, self.path)

        with self.assertRaises(NothingToPublish) as ctx:
            plan(self.publication)

        self.assertIn("no closed run", str(ctx.exception))

    def test_an_open_run_is_not_published(self):
        write_cogs(self.collection, self.path)
        make_run(self.collection, closed=False)

        with self.assertRaises(NothingToPublish):
            plan(self.publication)

    def test_the_latest_closed_run_wins(self):
        older = REFERENCE_TIME - timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=older)
        write_cogs(self.collection, self.path)
        make_run(self.collection, reference_time=older)
        make_run(self.collection)

        self.assertEqual(plan(self.publication).reference_time, REFERENCE_TIME)

    def test_the_version_carries_the_revision_so_a_republish_outranks_itself(self):
        write_cogs(self.collection, self.path)
        run = make_run(self.collection)
        first = plan(self.publication).version

        run.revision += 1
        run.save(update_fields=["revision"])

        self.assertGreater(plan(self.publication).version, first)

    def test_a_backfilled_older_run_never_outranks_a_newer_one(self):
        """``ref_epoch * 100`` leads, so the revision can only break ties."""
        older = REFERENCE_TIME - timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=older)
        old_run = make_run(self.collection, reference_time=older)
        old_run.revision = 99
        old_run.save(update_fields=["revision"])

        write_cogs(self.collection, self.path)
        new_run = make_run(self.collection)

        self.assertGreater(new_run.version, old_run.version)


class StepIntersectionTests(PlannerTestCase):
    def test_every_variable_present_means_every_step_publishes(self):
        write_cogs(self.collection, self.path, steps=9)
        make_run(self.collection)

        self.assertEqual(plan(self.publication).step_count, 9)

    def test_a_straggling_variable_shortens_the_run_for_everyone(self):
        """Publishing a step some variables lack would be a partial forecast."""
        write_cogs(self.collection, self.path, steps=9, skip=("tcc",))
        make_run(self.collection)

        self.assertEqual(plan(self.publication).step_count, 8)

    def test_no_shared_step_at_all_is_nothing_to_publish(self):
        write_cogs(self.collection, self.path, steps=1, skip=("tcc",))
        make_run(self.collection)

        with self.assertRaises(NothingToPublish) as ctx:
            plan(self.publication)

        self.assertIn("every variable", str(ctx.exception))

    def test_a_run_too_short_to_span_a_window_is_nothing_to_publish(self):
        """Instant values alone are a forecast with no rain and no symbol."""
        write_cogs(self.collection, self.path, steps=2)
        make_run(self.collection)

        with self.assertRaises(NothingToPublish) as ctx:
            plan(self.publication)

        self.assertIn("period window", str(ctx.exception))


class SourceValidationTests(PlannerTestCase):
    def test_a_missing_variable_is_refused_by_name(self):
        self.collection.variables.filter(slug="tcc").delete()
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("tcc", str(ctx.exception))

    def test_a_variable_in_the_wrong_unit_is_refused(self):
        kelvin, _ = Unit.objects.get_or_create(name="Kelvin", defaults={"symbol": "K"})
        variable = self.collection.variables.get(slug="2t")
        variable.unit = kelvin
        variable.save(update_fields=["unit"])
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("2t", str(ctx.exception))
        self.assertIn("without converting", str(ctx.exception))

    def test_a_non_public_collection_is_refused(self):
        self.collection.visibility = Collection.Visibility.PRIVATE
        self.collection.save(update_fields=["visibility"])
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("no credential", str(ctx.exception))


class ParameterSelectionTests(PlannerTestCase):
    def test_a_three_hourly_run_publishes_six_hour_series_and_no_one_hour_ones(self):
        write_cogs(self.collection, self.path, step_hours=3, steps=9)
        make_run(self.collection)

        names = {parameter.name for parameter in plan(self.publication).parameters}

        self.assertIn("precipitation_amount_acc6h", names)
        self.assertNotIn("precipitation_amount_acc1h", names)

    def test_the_fingerprint_changes_when_the_step_count_does(self):
        """A variable finishing late extends the intersection without touching
        the version, so the version alone cannot decide whether to rebuild."""
        write_cogs(self.collection, self.path, steps=9, skip=("tcc",))
        make_run(self.collection)
        short = plan(self.publication).fingerprint

        write_cogs(self.collection, self.path, steps=9)

        self.assertNotEqual(plan(self.publication).fingerprint, short)

    def test_the_fingerprint_is_stable_over_an_unchanged_run(self):
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        self.assertEqual(plan(self.publication).fingerprint, plan(self.publication).fingerprint)


class TimeUntilNextTests(PlannerTestCase):
    def test_it_is_measured_from_the_last_two_closed_runs(self):
        older = REFERENCE_TIME - timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=older)
        write_cogs(self.collection, self.path)
        make_run(self.collection, reference_time=older)
        make_run(self.collection)

        self.assertEqual(plan(self.publication).time_until_next, timedelta(hours=12))

    def test_one_run_is_nothing_to_measure(self):
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        self.assertIsNone(plan(self.publication).time_until_next)

    def test_an_explicit_override_wins(self):
        older = REFERENCE_TIME - timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=older)
        write_cogs(self.collection, self.path)
        make_run(self.collection, reference_time=older)
        make_run(self.collection)
        self.publication.time_until_next_hours = 6
        self.publication.save(update_fields=["time_until_next_hours"])

        self.assertEqual(plan(self.publication).time_until_next, timedelta(hours=6))
