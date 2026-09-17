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

from georiva.core.models import Asset, Collection, Unit, Variable
from georiva_publisher_forti.models import GENERATIONS_PER_REVISION, FortiPublication
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
        """``ref_epoch * 100`` leads *within the run version*, so the revision
        can only break ties. D25 multiplies this whole number by 100 again and
        adds the generation below it, which preserves the ordering exactly."""
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
    """What the mapping has to say before anything is read.

    The plan is pure database, so every refusal here costs milliseconds and
    arrives before the first of ~70 MB of windowed raster reads.
    """

    def _remap(self, slot, variable):
        self.publication.variable_mappings.filter(slot=slot).update(variable=variable)

    def test_a_blank_slot_is_refused_by_name(self):
        """The refusal a missing variable used to give. It now names the *slot*
        an operator fills in rather than the variable they would have to rename
        the rest of the instance to create."""
        self._remap("tcc", None)
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

    def test_the_unit_check_follows_the_mapping_rather_than_the_slug(self):
        """A variable named anything at all is checked against the slot it was
        mapped into — which is the only unit that can be compared, now that the
        name no longer says what the value means."""
        kelvin, _ = Unit.objects.get_or_create(name="Kelvin", defaults={"symbol": "K"})
        write_cogs(self.collection, self.path)
        self._remap("2t", _renamed_copy_of(self.collection, "2t", "temperature-2m", unit=kelvin))
        make_run(self.collection)

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("temperature-2m", str(ctx.exception))

    def test_two_spellings_of_one_unit_agree(self):
        """``°C`` and ``degC`` depend on which seed wrote the row and are the
        same unit; kelvin is compatible with both and is not."""
        degrees, _ = Unit.objects.get_or_create(symbol="°C", defaults={"name": "Celsius (degree sign)"})
        variable = self.collection.variables.get(slug="2t")
        variable.unit = degrees
        variable.save(update_fields=["unit"])
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        self.assertEqual(plan(self.publication).step_count, 9)

    def test_a_slot_mapped_outside_the_collection_is_refused(self):
        other = make_collection(slug="other-surface")
        self._remap("2t", other.variables.get(slug="2t"))
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("outside", str(ctx.exception))

    def test_a_variable_named_anything_publishes_once_it_is_mapped(self):
        """The whole point: a collection that does not use GeoRiva's
        conventional slugs was unpublishable and is now a mapping away."""
        write_cogs(self.collection, self.path)
        renamed = _renamed_copy_of(self.collection, "2t", "temperature-2m")
        self._remap("2t", renamed)
        self.collection.variables.get(slug="2t").delete()
        make_run(self.collection)

        self.assertEqual(plan(self.publication).step_count, 9)

    def test_one_variable_may_fill_two_slots(self):
        """Legal, suspicious, and not this seam's to refuse — the hrefs have to
        reach both slots rather than whichever the query returned last."""
        self._remap("2d", self.collection.variables.get(slug="2t"))
        write_cogs(self.collection, self.path)
        make_run(self.collection)

        publish_plan = plan(self.publication)

        self.assertEqual(publish_plan.hrefs["2d"], publish_plan.hrefs["2t"])

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

    def test_the_fingerprint_changes_when_the_mapping_does(self):
        """The same run read through a different variable is different bytes,
        and a skip check that could not tell would decline to write them."""
        write_cogs(self.collection, self.path)
        other = _renamed_copy_of(self.collection, "2t", "temperature-2m")
        make_run(self.collection)
        before = plan(self.publication).fingerprint

        self.publication.variable_mappings.filter(slot="2t").update(variable=other)

        self.assertNotEqual(plan(self.publication).fingerprint, before)


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
            status=FortiPublication.Status.READY,
            input_fingerprint=publish_plan.fingerprint,
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
        self.assertEqual(publish_plan.version, self.run.version * GENERATIONS_PER_REVISION)

    def test_raising_the_generation_raises_the_version(self):
        first = self._publish().version

        self._raise_generation(1)

        self.assertGreater(plan(self.publication).version, first)

    def test_a_new_run_outranks_any_generation_of_the_one_before_it(self):
        """Model time dominates, which is what D8 required and this preserves."""
        self._publish()
        self._raise_generation(GENERATIONS_PER_REVISION - 1)
        highest = self._publish().version

        later = REFERENCE_TIME + timedelta(hours=12)
        write_cogs(self.collection, self.path, reference_time=later)
        make_run(self.collection, reference_time=later)

        self.assertGreater(plan(self.publication).version, highest)

    def test_a_republish_of_the_run_outranks_the_generation_below_it(self):
        """Run republish sits between model time and configuration: a reopened
        run at generation 0 beats the same run's highest generation."""
        self._publish()
        self._raise_generation(GENERATIONS_PER_REVISION - 1)
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

    def test_a_stale_instance_does_not_reset_the_generation(self):
        """The generation is read from the row, not from the caller's copy.

        The build discipline writes every transition with a queryset
        ``update()``, so an instance held across one is stale by exactly the
        write that matters. A stale copy here reads "never published", resets to
        0, and moves the pointer *backwards* — the failure the whole mechanism
        exists to prevent.
        """
        stale = FortiPublication.objects.get(pk=self.publication.pk)
        self._publish()
        self._raise_generation(4)

        self.assertIsNone(stale.published_version)
        self.assertEqual(stale.generation, 0)
        self.assertEqual(plan(stale).generation, 4)

    def test_publishing_at_the_ceiling_is_refused_rather_than_wrapping(self):
        """A wrapped generation is not a smaller number — it is exactly the
        stamp the run's next revision would produce, which the reader reads as
        "I already have this"."""
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            published_version=plan(self.publication).version,
            generation=GENERATIONS_PER_REVISION,
        )
        self.publication.refresh_from_db()

        with self.assertRaises(PublicationRefused) as ctx:
            plan(self.publication)

        self.assertIn("generation", str(ctx.exception).lower())

    def test_the_ceiling_is_the_stamp_the_next_revision_claims(self):
        """The arithmetic the refusal exists for, asserted rather than trusted:
        this is a collision by *equality*, which strictly-greater cannot see."""
        wrapped = self.run.version * GENERATIONS_PER_REVISION + GENERATIONS_PER_REVISION
        next_revision = (self.run.version + 1) * GENERATIONS_PER_REVISION

        self.assertEqual(wrapped, next_revision)

    def test_the_fingerprint_changes_when_the_generation_does(self):
        """The skip check has to learn about configuration too: the run, the
        step count and the parameter names are all unmoved by a generation
        bump, so without this the publish returns early and the bump is never
        written."""
        before = self._publish().fingerprint

        self._raise_generation(1)

        self.assertNotEqual(plan(self.publication).fingerprint, before)

    def test_an_unchanged_publication_still_skips(self):
        """The other half of the criterion, and the half a fingerprint that
        changed on every plan would fail.

        Asserted through ``is_up_to_date`` rather than by comparing two plans to
        each other: what has to hold is that the fingerprint matches the one the
        *last publish stored*, because that comparison is the skip. Two plans
        agreeing with each other proves only that ``plan()`` is deterministic,
        which no bug in this ticket could have broken — and the sweep republishing
        an unchanged run every five minutes is what the skip prevents."""
        self._publish()

        self.assertTrue(self.publication.is_up_to_date(plan(self.publication).fingerprint))

    def test_a_raised_generation_stops_it_skipping(self):
        """The same assertion from the other side: the skip must *not* hold once
        the generation has moved, or the bump is never written."""
        self._publish()

        self._raise_generation(1)

        self.assertFalse(self.publication.is_up_to_date(plan(self.publication).fingerprint))


def _renamed_copy_of(collection, source: str, slug: str, unit=None):
    """The same variable under a name the parameter map has never heard of.

    What a mapping is *for*: a collection that calls its 2-metre temperature
    something else. Same COGs, so every step still resolves and only the name —
    and optionally the unit — differs. Called after ``write_cogs``, which knows
    a constant for each conventional slug and nothing about this one.
    """
    original = collection.variables.get(slug=source)
    copy = Variable.objects.create(
        collection=collection,
        slug=slug,
        name=slug,
        unit=unit or original.unit,
        value_min=original.value_min,
        value_max=original.value_max,
    )
    for asset in Asset.objects.filter(item__collection=collection, variable=original):
        Asset.objects.get_or_create(item=asset.item, variable=copy, href=asset.href, format=Asset.Format.COG)
    return copy
