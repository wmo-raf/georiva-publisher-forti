"""What a row of the build log means, which is not what its columns hold.

Every figure on :class:`~.models.FortiPublicationBuildLog` has a default of zero
and is filled in as the attempt proceeds, so a zero is ambiguous at the database
and unambiguous in practice: no publish this plugin can complete writes zero
objects, transposes zero points or publishes zero steps. A zero is therefore
**something the attempt never got to**, and printing it as a figure would report
a run that collapsed to nothing where the truth is a run that died before it
counted anything.

That single rule is most of this module. The rest is the two distinctions the
recorded columns leave implicit and an operator reads the page to make:

- a **retention pass** against a **publish attempt** — they share a table and
  have almost no figures in common, so a retention pass rendered in a publish's
  terms reads as a publish that wrote nothing at all;
- a publish with **nothing to do** against a publish that **wrote nothing**.
  Both are successes holding no objects, and the first is the normal outcome of
  re-queueing an up-to-date publication while the second has never happened.

The distinctions are asserted here, on the data, and only thinly at the page —
the same division the verification report and its panel already use.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from georiva_publisher_forti import history
from georiva_publisher_forti.models import FortiPublicationBuildLog

from .factories import make_collection, make_publication

BUILD = FortiPublicationBuildLog.Kind.BUILD
GC = FortiPublicationBuildLog.Kind.GC
SUCCESS = FortiPublicationBuildLog.Outcome.SUCCESS
FAILURE = FortiPublicationBuildLog.Outcome.FAILURE

#: One whole publish, in the terms ``publisher.publish`` fills the attempt dict.
PUBLISHED = {
    "version": 178835040000,
    "step_count": 15,
    "point_count": 1920,
    "parameter_count": 15,
    "objects_written": 4,
}


class HistoryTestCase(TestCase):
    def setUp(self):
        self.publication = make_publication(make_collection(), slug="ecmwf-ifs")

    def record(self, kind=BUILD, outcome=SUCCESS, ago=0, publication=None, **fields):
        """One row, stamped far enough apart that ordering is not a coin toss."""
        started_at = timezone.now() - timedelta(minutes=ago)
        row = FortiPublicationBuildLog.record(
            publication or self.publication,
            kind,
            outcome,
            started_at,
            **fields,
        )
        return row

    def only(self, publication=None):
        (entry,) = history.report(publication or self.publication).entries
        return entry

    def figures(self, entry):
        return {figure.label: figure.value for figure in entry.figures}


class PublishAttemptTests(HistoryTestCase):
    def test_a_publish_shows_every_figure_it_recorded(self):
        self.record(**PUBLISHED)

        entry = self.only()

        self.assertEqual(entry.kind_label, history.PUBLISH_LABEL)
        self.assertEqual(entry.badge, history.PUBLISHED)
        self.assertEqual(
            self.figures(entry),
            {
                "version": 178835040000,
                "steps": 15,
                "points": 1920,
                "parameters": 15,
                "objects written": 4,
            },
        )

    def test_a_publish_that_pruned_says_so_beside_what_it_wrote(self):
        """Retention runs at the tail of every publish as well as on its own
        daily pass, so this figure belongs to both kinds of row."""
        self.record(**PUBLISHED, versions_pruned=2)

        self.assertEqual(self.figures(self.only())["old versions removed"], 2)

    def test_a_publish_that_pruned_nothing_does_not_claim_a_figure(self):
        """Nearly every publish prunes nothing — there are five slots and a run
        a day fills one — so a column of "versions pruned: 0" would be the most
        repeated figure on the page and mean nothing on any row."""
        self.record(**PUBLISHED)

        self.assertNotIn("old versions removed", self.figures(self.only()))


class NothingToDoTests(HistoryTestCase):
    """The success that wrote nothing, which is not a publish that failed to.

    ``publish`` fills in the version, the step count and the parameter count
    before it asks whether the fingerprint has moved, then returns early — so an
    up-to-date publication records a success holding three figures and no
    objects. That is the ordinary outcome of the re-queue button on a
    publication that is already current, and reading it as a publish that wrote
    nothing would send an operator looking for a fault that is not there.
    """

    def test_an_up_to_date_publish_is_told_apart_from_one_that_wrote(self):
        self.record(version=178835040000, step_count=15, parameter_count=15)

        entry = self.only()

        self.assertEqual(entry.badge, history.UNCHANGED)
        self.assertNotEqual(entry.badge, history.PUBLISHED)

    def test_it_keeps_the_figures_it_did_establish(self):
        """The version is the useful half: it names the run the publication was
        found to be already holding."""
        self.record(version=178835040000, step_count=15, parameter_count=15)

        self.assertEqual(
            self.figures(self.only()),
            {"version": 178835040000, "steps": 15, "parameters": 15},
        )


class FailedAttemptTests(HistoryTestCase):
    def test_a_failure_carries_its_error(self):
        self.record(outcome=FAILURE, version=178835040000, step_count=15, error="GridMoved: 1920 points, pinned at 480")

        entry = self.only()

        self.assertEqual(entry.badge, history.FAILED)
        self.assertEqual(entry.error, "GridMoved: 1920 points, pinned at 480")

    def test_a_figure_the_attempt_never_reached_is_absent_rather_than_zero(self):
        """The whole of the zero rule, on the row that makes it matter. This
        attempt died before it read a single raster; "points: 0" would describe
        an area that produced no points, which is a different fault with a
        different cause and no relation to what happened here."""
        self.record(outcome=FAILURE, version=178835040000, step_count=15, parameter_count=15, error="boom")

        figures = self.figures(self.only())

        self.assertEqual(figures, {"version": 178835040000, "steps": 15, "parameters": 15})
        self.assertNotIn("points", figures)
        self.assertNotIn("objects written", figures)

    def test_a_failure_whose_exception_said_nothing_still_says_something(self):
        """``build_attempt`` stores ``str(exc)`` whatever it is, and ``str()`` of
        a bare ``ValueError()`` is the empty string. With no figures either — a
        failure this early has established none — the row would otherwise render
        as blank space where the error belongs, which is the one row on the page
        that must never be silent."""
        self.record(outcome=FAILURE, error="")

        entry = self.only()

        self.assertEqual(entry.badge, history.FAILED)
        self.assertEqual(entry.error, history.NO_MESSAGE)

    def test_a_success_is_not_given_a_message_it_never_had(self):
        """The fallback is a failure's, and a success with an empty error field
        is every success there has ever been."""
        self.record(**PUBLISHED)

        self.assertEqual(self.only().error, "")

    def test_an_attempt_that_established_nothing_has_no_figures_at_all(self):
        """A publication with no closed run never reaches the plan, so there is
        genuinely nothing to say about it but the error."""
        self.record(outcome=FAILURE, error="NothingToPublish: has no closed run")

        entry = self.only()

        self.assertEqual(entry.figures, ())
        self.assertEqual(entry.error, "NothingToPublish: has no closed run")


class RetentionPassTests(HistoryTestCase):
    def test_a_retention_pass_is_labelled_as_one(self):
        self.record(kind=GC, versions_pruned=3)

        entry = self.only()

        self.assertEqual(entry.kind_label, history.RETENTION_LABEL)
        self.assertNotEqual(entry.kind_label, history.PUBLISH_LABEL)
        self.assertEqual(entry.badge, history.PRUNED)

    def test_it_is_described_in_its_own_terms_and_not_a_publish_s(self):
        """A retention pass shares every column with a publish and fills one of
        them. Rendered in a publish's terms it is a publish that produced no
        version, no steps and no points — which is what a failed publish looks
        like."""
        self.record(kind=GC, versions_pruned=3)

        self.assertEqual(self.figures(self.only()), {"old versions removed": 3})

    def test_a_retention_pass_that_failed_carries_its_error_too(self):
        self.record(kind=GC, outcome=FAILURE, error="ClientError: An error occurred (AccessDenied)")

        entry = self.only()

        self.assertEqual(entry.kind_label, history.RETENTION_LABEL)
        self.assertEqual(entry.badge, history.FAILED)
        self.assertEqual(entry.error, "ClientError: An error occurred (AccessDenied)")


class OrderAndScopeTests(HistoryTestCase):
    def test_the_most_recent_attempt_is_first(self):
        """An operator opens this page to answer "what happened just now", and
        reads down from there into however much of the past they need."""
        self.record(ago=90, **PUBLISHED)
        self.record(ago=30, outcome=FAILURE, error="the most recent thing that happened")
        self.record(ago=60, kind=GC, versions_pruned=1)

        report = history.report(self.publication)

        self.assertEqual(
            [entry.error or entry.badge for entry in report.entries],
            ["the most recent thing that happened", history.PRUNED, history.PUBLISHED],
        )

    def test_another_publication_s_attempts_are_not_in_this_history(self):
        """The narrowing that makes this an organisation administrator's page at
        all: a history is one publication's, and the row carries the foreign
        key that says so."""
        theirs = make_publication(make_collection(slug="gfs-surface", org_slug="other-org"), slug="gfs")
        self.record(publication=theirs, outcome=FAILURE, error="not this organisation's failure")
        self.record(**PUBLISHED)

        report = history.report(self.publication)

        self.assertEqual(len(report.entries), 1)
        self.assertEqual(report.entries[0].badge, history.PUBLISHED)

    def test_a_publication_with_no_history_is_empty_rather_than_missing(self):
        """Every publication is in this state until its first sweep, so it is
        the first state the page is ever seen in."""
        report = history.report(self.publication)

        self.assertEqual(report.entries, ())
        self.assertEqual(report.total, 0)
        self.assertFalse(report.truncated)


class LengthTests(HistoryTestCase):
    """Retention keeps thirty days, and a busy publication fills them.

    Six runs a day for a month is nearly two hundred rows, each with an error
    that may run to a traceback's first line. The page shows the recent ones and
    says how many there are, rather than rendering every row a daily prune
    happens not to have reached yet.
    """

    def test_a_long_history_shows_the_recent_ones_and_counts_the_rest(self):
        now = timezone.now()
        FortiPublicationBuildLog.objects.bulk_create(
            FortiPublicationBuildLog(
                publication=self.publication,
                kind=BUILD,
                outcome=SUCCESS,
                started_at=now - timedelta(minutes=minutes),
                finished_at=now - timedelta(minutes=minutes) + timedelta(seconds=30),
                **PUBLISHED,
            )
            for minutes in range(history.SHOWN_ROWS + 5)
        )

        report = history.report(self.publication)

        self.assertEqual(len(report.entries), history.SHOWN_ROWS)
        self.assertEqual(report.total, history.SHOWN_ROWS + 5)
        self.assertTrue(report.truncated)

    def test_a_history_that_fits_does_not_claim_to_be_cut_short(self):
        self.record(**PUBLISHED)

        report = history.report(self.publication)

        self.assertEqual(report.total, 1)
        self.assertFalse(report.truncated)


class DurationTests(HistoryTestCase):
    """How long an attempt took, which is how a run getting slower is noticed.

    Rounded rather than exact: the figure is read down a column to see a trend,
    and six significant figures of microseconds would hide the trend inside the
    noise.
    """

    def test_a_publish_of_a_few_seconds_is_said_in_seconds(self):
        row = self.record(**PUBLISHED)
        row.finished_at = row.started_at + timedelta(seconds=8.4)
        row.save(update_fields=["finished_at"])

        self.assertEqual(self.only().duration, "8 s")

    def test_a_publish_of_minutes_is_said_in_minutes(self):
        row = self.record(**PUBLISHED)
        row.finished_at = row.started_at + timedelta(minutes=4, seconds=30)
        row.save(update_fields=["finished_at"])

        self.assertEqual(self.only().duration, "4 min 30 s")

    def test_a_publish_of_hours_drops_the_seconds_rather_than_counting_to_7200(self):
        """Reachable: a build holds its lock for as long as it needs, and a
        wide area over a slow bucket has taken this long. Seconds stop meaning
        anything at this scale and "134 min" is a figure nobody reads."""
        row = self.record(**PUBLISHED)
        row.finished_at = row.started_at + timedelta(hours=2, minutes=14, seconds=9)
        row.save(update_fields=["finished_at"])

        self.assertEqual(self.only().duration, "2 h 14 min")
