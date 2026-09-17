"""Readiness, and the one distinction it exists to draw.

Everything here is about telling **"not ready yet"** from **"will never work"**.
A collection configured the afternoon before its first ingestion is not
misconfigured — it is early, and a surface that says "cannot publish" about it
sends an operator looking for a fault that does not exist. A publication whose
2t slot is blank is the opposite: nothing that happens on its own will ever fix
it, and a surface that said "not ready yet" would have an operator waiting for a
run that has already landed.

So the tests below are organised around states rather than around findings. Each
one puts the publication in a condition and asserts which word comes back for
it, because the word is the feature: the figures beside it are a reading of rows
this plugin already had.

No rendering here. :mod:`~.tests.test_panel` asserts that the page carries what
this module decides, thinly and once, which is the split every other surface in
this plugin makes.
"""

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase

from georiva.core.models import Collection, Unit
from georiva_publisher_forti import readiness

from .factories import REFERENCE_TIME, make_collection, make_publication, make_run, write_cogs


class ReadinessTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.collection = make_collection()
        self.publication = make_publication(self.collection)

    def finding(self, report, subject):
        """One finding by its key, so a test names what it is asserting about."""
        return next(finding for finding in report.findings if finding.key == subject)

    # -- the distinction -----------------------------------------------------

    def test_a_collection_with_no_closed_run_is_not_ready_yet(self):
        """The state every publication is born in, and a legitimate one: a
        publication may be configured the afternoon before its first ingestion.
        Nothing here needs changing — a run closing is what fixes it."""
        report = readiness.report(self.publication)

        self.assertEqual(self.finding(report, "runs").state, readiness.WAITING)
        self.assertEqual(report.state, readiness.WAITING)
        self.assertFalse(report.can_publish)

    def test_a_blank_slot_will_never_publish_on_its_own(self):
        """The other half of the distinction. Waiting fixes a run that has not
        closed; nothing fixes a slot nothing fills, and an operator told to wait
        for one would wait for as long as they were willing to."""
        make_run(self.collection)
        self.publication.variable_mappings.filter(slot="2t").update(variable=None)

        report = readiness.report(self.publication)

        self.assertEqual(self.finding(report, "slots").state, readiness.BLOCKED)
        self.assertIn("2t", self.finding(report, "slots").detail)
        self.assertEqual(report.state, readiness.BLOCKED)
        self.assertFalse(report.can_publish)

    def test_a_unit_that_disagrees_is_a_change_rather_than_a_wait(self):
        """Forti copies units out of ``meta.json`` without converting, so this
        is the refusal that would otherwise be met at the first publish — and
        the one an operator most needs to meet before it, because the publish it
        stops is the one that would have served a number wrong by 273."""
        make_run(self.collection)
        kelvin, _ = Unit.objects.get_or_create(name="Kelvin", defaults={"symbol": "K"})
        variable = self.collection.variables.get(slug="2t")
        variable.unit = kelvin
        variable.save(update_fields=["unit"])

        report = readiness.report(self.publication)

        units = self.finding(report, "units")
        self.assertEqual(units.state, readiness.BLOCKED)
        self.assertIn("2t", units.detail)
        self.assertIn("K", units.detail)
        self.assertFalse(report.can_publish)

    def test_a_complete_run_says_how_many_steps_it_would_publish(self):
        """The figure the build log carried and nothing rendered. An operator
        who can read it before saving learns what a publish would do from the
        form rather than from what happened afterwards."""
        write_cogs(self.collection, self.path, steps=9)
        make_run(self.collection)

        report = readiness.report(self.publication)

        steps = self.finding(report, "steps")
        self.assertEqual(steps.state, readiness.READY)
        self.assertIn("9", steps.answer)
        self.assertEqual(report.state, readiness.READY)
        self.assertTrue(report.can_publish)

    def test_a_run_whose_stragglers_have_not_landed_publishes_fewer_steps(self):
        """The number an operator otherwise discovers by publishing. Variables
        of one run ingest independently — 13 to 16 of 16 on a live run — so this
        is the ordinary state of a run mid-arrival, and it publishes: what it
        does not do is publish everything it eventually will."""
        write_cogs(self.collection, self.path, steps=9, skip=("tp",))
        make_run(self.collection)

        report = readiness.report(self.publication)

        steps = self.finding(report, "steps")
        self.assertEqual(steps.state, readiness.PARTIAL)
        self.assertEqual(steps.answer, "8 of 9")
        self.assertIn("tp", steps.detail)
        self.assertEqual(report.state, readiness.PARTIAL)
        self.assertTrue(report.can_publish)

    def test_a_collection_that_stopped_being_a_forecast_is_reported_as_such(self):
        """The chooser offers forecast collections and the model refuses the
        rest, so the way a publication reaches this state is the collection
        being changed under it. Nothing downstream would say so: the planner
        refuses at the run, which reads as a run that has not closed."""
        make_run(self.collection)
        self.collection.is_forecast = False
        self.collection.save(update_fields=["is_forecast"])

        report = readiness.report(self.publication)

        forecast = self.finding(report, "forecast")
        self.assertEqual(forecast.state, readiness.BLOCKED)
        self.assertEqual(report.state, readiness.BLOCKED)

    def test_a_collection_no_reader_may_read_is_a_change_rather_than_a_wait(self):
        """A Forti reader presents no credential, so the planner refuses a
        collection that is not public. Nothing on the form said so: the
        publication saves, inherits the narrower tier, and is refused at the
        first build — the one refusal a readiness page that listed only the
        ticket's five findings would have left to be discovered that way."""
        collection = make_collection(slug="ifs-restricted", visibility=Collection.Visibility.PRIVATE)
        publication = make_publication(collection, slug="restricted")
        make_run(collection)

        report = readiness.report(publication)

        visibility = self.finding(report, "visibility")
        self.assertEqual(visibility.state, readiness.BLOCKED)
        self.assertIn("private", visibility.answer)
        self.assertEqual(report.state, readiness.BLOCKED)

    def test_the_closed_runs_are_counted_and_the_latest_is_named(self):
        """Both halves matter and neither is the other. The count says whether
        this collection ingests at all; the reference time says whether it is
        still doing so, which a count of forty from last March does not."""
        make_run(self.collection, reference_time=REFERENCE_TIME - timedelta(hours=12))
        make_run(self.collection)
        make_run(self.collection, reference_time=REFERENCE_TIME + timedelta(hours=12), closed=False)

        runs = self.finding(readiness.report(self.publication), "runs")

        self.assertEqual(runs.state, readiness.READY)
        self.assertEqual(runs.answer, "2, latest 2026-09-02T12:00Z")

    def test_the_step_count_of_a_collection_with_no_run_is_not_a_fault(self):
        """The third state, and the reason it exists. A publication waiting for
        its first run has nothing wrong with its steps — there are no steps to
        have anything wrong with — and a red row here would be a second thing to
        fix where there is not yet a first."""
        report = readiness.report(self.publication)

        self.assertEqual(self.finding(report, "steps").state, readiness.UNANSWERABLE)
        self.assertEqual(report.state, readiness.WAITING)

    def test_a_run_too_short_to_span_a_window_waits_rather_than_refuses(self):
        """Instant values alone are a forecast with no precipitation and no
        symbol, which is why the planner refuses one. It is still a run in mid
        arrival: more steps land, a 6-hour window appears, and nobody has to
        change anything for that to happen."""
        write_cogs(self.collection, self.path, steps=2, step_hours=3)
        make_run(self.collection)

        steps = self.finding(readiness.report(self.publication), "steps")

        self.assertEqual(steps.state, readiness.WAITING)
        self.assertIn("window", steps.detail)

    def test_a_run_no_slot_shares_a_step_with_waits(self):
        """Variables of one run ingest independently, so a run whose slots have
        landed at disjoint steps is early rather than broken."""
        write_cogs(self.collection, self.path, steps=1)
        self.collection.variables.get(slug="tp").assets.all().delete()
        make_run(self.collection)

        steps = self.finding(readiness.report(self.publication), "steps")

        self.assertEqual(steps.state, readiness.WAITING)
        self.assertEqual(steps.answer, "none yet")
