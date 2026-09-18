"""One publication's publish attempts and retention passes, made readable.

``FortiPublicationBuildLog`` has recorded every attempt since the plugin's first
publish and nothing has ever rendered a row of it. The publication itself holds
only the latest state — a failed build overwrites the previous error in place —
so an operator could tell that the last publish failed and could not tell a
first failure from a week of them, or see a run getting smaller until somebody
complained.

Nothing here records, reads remotely or decides anything about publishing. It is
a reading of rows that already exist, and it exists as a module rather than as
logic in the template for the reason the verification report does: the
distinctions below are the whole substance of the feature, and a distinction
drawn inside ``{% if %}`` is a distinction nobody tests.

**The zero rule.** Every figure on the log defaults to zero and is filled in as
the attempt proceeds, so ``0`` at the database means either "counted zero" or
"never got this far" — and in practice only ever the second. No publish this
plugin can complete writes zero objects, transposes zero points or publishes
zero steps: an area with no points is refused at the grid and a run with no
steps never reaches the plan. So a zero is omitted rather than printed, and
what a row shows is exactly how far the attempt got. The alternative reports an
attempt that died before it counted anything as a run that collapsed to nothing,
which is a different fault with a different cause.

**The two distinctions the columns leave implicit.** A retention pass shares
every column with a publish and fills one of them, so described in a publish's
terms it is a publish with no version, no steps and no points — which is what a
*failed* publish looks like. And a publish that found itself up to date returns
before it writes anything, recording a success holding three figures and no
objects; that is the ordinary outcome of the re-queue button on a current
publication, and reading it as a publish that wrote nothing would send somebody
looking for a fault that is not there.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

#: A ceiling, not an editorial decision. What bounds this page in normal
#: operation is retention: rows older than thirty days are deleted by the daily
#: pass, and thirty days of the busiest plausible cadence — four runs a day and
#: a retention pass — is under two hundred rows. So this sits above that, and
#: what it actually guards is the deployment where the daily task is *not*
#: running: rows then accumulate for as long as nobody notices, and the page an
#: operator would use to notice must not be the one that stops loading. When it
#: does bite, the page says how many rows it is not showing rather than ending
#: without explanation.
SHOWN_ROWS = 250

#: The badge a row gets, which is the state rather than the outcome column: two
#: of these are ``success``, and telling them apart is half of why this module
#: exists.
PUBLISHED = "published"
UNCHANGED = "unchanged"
PRUNED = "pruned"
FAILED = "failed"

#: What each badge says out loud. "Nothing to do" rather than "unchanged"
#: because the row is an *attempt*, and what an operator wants to know is what
#: the attempt did — not what it found.
BADGE_LABELS = {
    PUBLISHED: "published",
    UNCHANGED: "nothing to do",
    PRUNED: "cleaned up",
    FAILED: "failed",
}

#: The colour each badge is emphasised in, in Wagtail's own utility classes and
#: beside its own ``w-status`` pill — which is how :class:`~.wagtail_hooks.ResidencyColumn`
#: already colours six words, and the reason it does: a plugin has no business
#: adding admin CSS for what the admin already has. The word carries the state
#: and the colour only emphasises it, so a badge with no rule here renders in the
#: table's ordinary text rather than in somebody else's colour.
BADGE_CLASSES = {
    PUBLISHED: "w-text-positive-100",
    PRUNED: "w-text-positive-100",
    UNCHANGED: "w-text-grey-400",
    FAILED: "w-text-critical-200",
}

#: What a failure says when the exception did not. ``build_attempt`` stores
#: ``str(exc)`` whatever it is, and ``str()`` of a bare ``ValueError()`` is the
#: empty string — so without this a failed attempt that established no figures
#: renders as an empty row, which is the one row on the page that must never be
#: silent.
NO_MESSAGE = "Failed without a message. Ask your administrator to check the logs."

PUBLISH_LABEL = "Publish"
RETENTION_LABEL = "Clean-up"

#: Every figure a row can carry, in the order a publish establishes them, under
#: the names the plan and the bucket use rather than the column names. One
#: sequence for both kinds: a retention pass reaches only the last of them, and
#: a publish that pruned reaches it too, so splitting the list per kind would
#: put one figure in two places.
FIGURES = (
    ("version", "version"),
    ("step_count", "steps"),
    ("point_count", "points"),
    ("parameter_count", "parameters"),
    ("objects_written", "objects written"),
    ("versions_pruned", "old versions removed"),
)


@dataclass(frozen=True)
class Figure:
    """One number an attempt established, under the name an operator knows it by."""

    label: str
    value: int

    @property
    def text(self) -> str:
        """The figure as the page prints it, unlocalised for the reason
        :attr:`~.verification.Residency.figure` gives: a version is an
        identifier that happens to be spelled in digits. The counts beside it
        are too small for a separator to ever appear, so one rule covers every
        figure rather than two covering one each."""
        return str(self.value)


@dataclass(frozen=True)
class Entry:
    """One publish attempt, or one retention pass."""

    kind_label: str
    badge: str
    started_at: datetime
    duration: str
    figures: tuple[Figure, ...]
    error: str = ""

    @property
    def badge_label(self) -> str:
        return BADGE_LABELS.get(self.badge, self.badge)

    @property
    def badge_class(self) -> str:
        return BADGE_CLASSES.get(self.badge, "")


@dataclass(frozen=True)
class History:
    """What one publication has done, and how much of it is being shown."""

    entries: tuple[Entry, ...] = ()
    #: Rows the publication has, not rows shown — the difference is the whole
    #: point of saying it.
    total: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > len(self.entries)


def report(publication) -> History:
    """One publication's history, most recent first.

    Most recent first because the question the page is opened with is "what
    happened just now", and everything else is read downwards from there.

    Two queries: the rows shown, and a count of the rest. The count is separate
    rather than ``len()`` of an unsliced queryset, because "how much am I not
    seeing" must not cost the fetching of what is not being seen.
    """
    rows = publication.build_logs.all()[:SHOWN_ROWS]
    entries = tuple(_entry(row) for row in rows)
    total = publication.build_logs.count() if len(entries) == SHOWN_ROWS else len(entries)
    return History(entries=entries, total=total)


def _entry(row) -> Entry:
    is_retention = row.kind == row.Kind.GC
    return Entry(
        kind_label=RETENTION_LABEL if is_retention else PUBLISH_LABEL,
        badge=_badge(row, is_retention),
        started_at=row.started_at,
        duration=_duration(row.duration),
        figures=_figures(row),
        error=_error(row),
    )


def _error(row) -> str:
    """What a failed row says, which is never nothing. See :data:`NO_MESSAGE`."""
    if row.outcome != row.Outcome.FAILURE:
        return row.error
    return row.error or NO_MESSAGE


def _badge(row, is_retention: bool) -> str:
    """Four states out of two columns, decided here and not in the template.

    A retention pass that pruned nothing is never recorded —
    ``prune_forti_publications`` writes a success row only when something went —
    so every successful pass has a figure, and ``pruned`` needs no counterpart
    to ``unchanged``.

    ``unchanged`` is **inferred**, and the invariant it rests on is one line of
    somebody else's function: ``publisher.publish`` sets
    ``facts["objects_written"]`` only after ``stage_version`` has returned, and
    returns before that whenever the fingerprint has not moved. So a success
    holding no objects is a publish that found itself up to date, and there is
    no other path to one. A future success path that wrote nothing would read as
    "nothing to do" here without saying so anywhere — which is why the coupling
    is named rather than left to be rediscovered. Recording the skip explicitly
    would settle it, and is a change to what is recorded rather than to what is
    rendered.
    """
    if row.outcome == row.Outcome.FAILURE:
        return FAILED
    if is_retention:
        return PRUNED
    return PUBLISHED if row.objects_written else UNCHANGED


def _figures(row) -> tuple[Figure, ...]:
    """The figures this attempt actually established. See the zero rule above."""
    return tuple(
        Figure(label=label, value=getattr(row, field))
        for field, label in FIGURES
        if getattr(row, field)  # 0 and None alike mean "never got this far".
    )


def _duration(elapsed: timedelta) -> str:
    """How long it took, rounded to what a trend is visible in.

    Read down a column rather than individually — a run that has grown from
    forty seconds to four minutes is the thing worth seeing — so the figure is
    coarse on purpose. Microseconds would hide that inside their own noise.
    """
    seconds = int(elapsed.total_seconds())
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"
