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
from georiva_publisher_forti.models import GENERATION_CEILING, FortiPublication
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


class GenerationTests(PlannerTestCase):
    """The half of the version that counts what the run did not change.

    ``rawdataforecaster`` reloads only on a strictly greater version
    (``forecast.go:293``). A publication republished under a changed
    configuration writes different bytes for the same run, so without a term
    that moves independently of the run the pointer keeps its old integer and
    the reader never loads them — while the panel shows published, available
    and loaded all equal and reports agreement.
    """

    def setUp(self):
        super().setUp()
        write_cogs(self.collection, self.path)
        self.run = make_run(self.collection)

    def _publish(self):
        """Record what a publish would leave on the row, and return the plan.

        The publisher's ``mark_ready``, minus the bytes. The generation is read
        back off the *plan* rather than off the row, exactly as the publisher
        does, so a reset the planner decided is what gets stored — which is what
        makes the row's stored generation and its published stamp agree.
        """
        publish_plan = plan(self.publication)
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            published_version=publish_plan.version,
            generation=publish_plan.generation,
        )
        self.publication.refresh_from_db()
        return publish_plan

    def _raise_generation(self, to: int) -> None:
        self.publication.generation = to
        self.publication.save(update_fields=["generation"])

    def test_the_version_is_the_run_version_with_the_generation_below_it(self):
        publish_plan = plan(self.publication)

        self.assertEqual(publish_plan.run_version, self.run.version)
        self.assertEqual(publish_plan.generation, 0)
        self.assertEqual(publish_plan.version, self.run.version * GENERATION_CEILING)

    def test_raising_the_generation_raises_the_version(self):
        first = self._publish().version

        self._raise_generation(1)

        self.assertGreater(plan(self.publication).version, first)

    def test_a_new_run_outranks_any_generation_of_the_one_before_it(self):
        """Model time dominates, which is what D8 required and this preserves."""
        self._publish()
        self._raise_generation(GENERATION_CEILING - 1)
        highest = self._publish().version

        later = REFERENCE_TIME + timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=later)
        make_run(self.collection, reference_time=later)

        self.assertGreater(plan(self.publication).version, highest)

    def test_a_republish_of_the_run_outranks_the_generation_below_it(self):
        """Run republish sits between model time and configuration: a reopened
        run at generation 0 beats the same run's highest generation."""
        self._publish()
        self._raise_generation(GENERATION_CEILING - 1)
        highest = self._publish().version

        self.run.revision += 1
        self.run.save(update_fields=["revision"])

        self.assertGreater(plan(self.publication).version, highest)

    def test_the_generation_resets_when_a_new_run_is_published(self):
        self._publish()
        self._raise_generation(4)
        self._publish()

        later = REFERENCE_TIME + timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=later)
        make_run(self.collection, reference_time=later)

        self.assertEqual(plan(self.publication).generation, 0)

    def test_the_generation_survives_a_republish_of_the_same_run(self):
        """The counterpart of the reset: it resets per *run*, so planning the
        run it was raised against again still carries it. Without this the
        reset would be indistinguishable from never reading the field at all."""
        self._publish()
        self._raise_generation(4)
        self._publish()

        self.assertEqual(plan(self.publication).generation, 4)

    def test_a_run_that_has_never_published_starts_at_generation_zero(self):
        """There is nothing to supersede yet, so a generation typed in before
        the first publish names no bytes and is not carried into one."""
        self._raise_generation(4)

        self.assertEqual(plan(self.publication).generation, 0)

    def test_publishing_at_the_ceiling_is_refused_rather_than_wrapping(self):
        """A wrapped generation is not a smaller number — it is exactly the
        stamp the run's next revision would produce, which the reader reads as
        "I already have this"."""
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            published_version=plan(self.publication).version,
            generation=GENERATION_CEILING,
        )
        self.publication.refresh_from_db()

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("generation", str(ctx.exception).lower())

    def test_the_ceiling_is_the_stamp_the_next_revision_claims(self):
        """The arithmetic the refusal exists for, asserted rather than trusted:
        this is a collision by *equality*, which strictly-greater cannot see."""
        wrapped = self.run.version * GENERATION_CEILING + GENERATION_CEILING
        next_revision = (self.run.version + 1) * GENERATION_CEILING

        self.assertEqual(wrapped, next_revision)

    def test_the_fingerprint_changes_when_the_generation_does(self):
        """The skip check has to learn about configuration too: the run, the
        step count and the parameter names are all unmoved by a generation
        bump, so without this the publish returns early and the bump is never
        written."""
        before = self._publish().fingerprint

        self._raise_generation(1)

        self.assertNotEqual(plan(self.publication).fingerprint, before)

    def test_the_fingerprint_is_unmoved_by_anything_that_did_not_change(self):
        """The other half of the criterion. A fingerprint that changed on every
        plan would never skip, and the skip is what stops the sweep republishing
        an unchanged run every five minutes."""
        self._publish()
        self._raise_generation(1)

        self.assertEqual(plan(self.publication).fingerprint, plan(self.publication).fingerprint)
