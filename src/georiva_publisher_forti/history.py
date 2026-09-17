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

#: How many rows the page shows. Retention keeps thirty days and a six-run day
#: fills nearly two hundred of them, each able to carry a traceback's first
#: line — so the page shows the recent ones and says how many there are. An
#: operator chasing further back than this is reading logs, not an admin page.
SHOWN = 50

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
    PRUNED: "pruned",
    FAILED: "failed",
}

PUBLISH_LABEL = "Publish"
RETENTION_LABEL = "Retention"

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
    ("versions_pruned", "versions pruned"),
)


@dataclass(frozen=True)
class Figure:
    """One number an attempt established, under the name an operator knows it by."""

    label: str
    value: int

    @property
    def text(self) -> str:
        """The figure as the page prints it, and never localised.

        The version is an identifier that happens to be spelled in digits — it
        is compared by eye against the listing's published version and pasted
        into a bucket path, so ``USE_THOUSAND_SEPARATOR`` turning it into
        ``178,835,040,000`` would make the page disagree with the bucket. The
        counts beside it are small enough that a separator would never appear,
        so one rule covers every figure rather than two covering one each.
        """
        return str(self.value)


@dataclass(frozen=True)
class Entry:
    """One publish attempt, or one retention pass."""

    kind_label: str
    is_retention: bool
    badge: str
    started_at: datetime
    duration: str
    figures: tuple[Figure, ...]
    error: str = ""

    @property
    def badge_label(self) -> str:
        return BADGE_LABELS.get(self.badge, self.badge)

    @property
    def failed(self) -> bool:
        return self.badge == FAILED


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
    rows = publication.build_logs.all()[:SHOWN]
    entries = tuple(_entry(row) for row in rows)
    total = publication.build_logs.count() if len(entries) == SHOWN else len(entries)
    return History(entries=entries, total=total)


def _entry(row) -> Entry:
    is_retention = row.kind == row.Kind.GC
    return Entry(
        kind_label=RETENTION_LABEL if is_retention else PUBLISH_LABEL,
        is_retention=is_retention,
        badge=_badge(row, is_retention),
        started_at=row.started_at,
        duration=_duration(row.duration),
        figures=_figures(row),
        error=row.error,
    )


def _badge(row, is_retention: bool) -> str:
    """Four states out of two columns, decided here and not in the template.

    A retention pass that pruned nothing is never recorded —
    ``prune_forti_publications`` writes a success row only when something went —
    so every successful pass has a figure, and ``pruned`` needs no counterpart
    to ``unchanged``.
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
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"
