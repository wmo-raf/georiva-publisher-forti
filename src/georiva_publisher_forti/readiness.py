"""Whether this publication would publish, asked before anybody publishes it.

Everything below was already decided somewhere — by
:func:`~.planner.plan`, at the moment of the first publish, into a build log
nothing renders. An operator who had configured a publication correctly and one
who had configured it a day early read the same page, and both of them found out
by waiting.

What this module adds is not a new rule. It is the reading of rules that already
exist, made before the fact rather than after it, and organised around the one
distinction an operator actually needs:

**"Not ready yet" is not "will never work".** A collection configured the
afternoon before its first ingestion has nothing wrong with it — a run closing
is what fixes it, and the operator's correct action is to wait. A publication
with a blank slot is the opposite: no amount of waiting fixes it, and every hour
spent waiting is an hour the model is not served. Those two states are one word
apart on the page and are opposite instructions, which is why they are separate
constants here and why the tests are organised around them rather than around
the findings.

**A module returning data, not logic in a template.** The same arrangement
:mod:`~.verification` and :mod:`~.history` already use, for the same reason: a
distinction drawn inside ``{% if %}`` is a distinction nobody tests, and the
distinctions *are* the feature.

## What it asks, and why one more than the ticket named

Six findings. Five are the ones an operator meets in order — whether the
collection is a forecast, how many closed runs it has and when the latest was,
whether every slot resolves, whether the units agree, and how many steps the
latest run would actually publish.

The sixth is the collection's **visibility**, and it is here because
:func:`~.planner._refuse_unpublishable_collection` refuses a collection that is
not public. A readiness page that omitted it would report "ready" about a
publication the very next build refuses — which is precisely the fault this
module exists to repair, recreated one refusal further down.

## What it does not ask

It reads the database and nothing else: no bucket, no status document, no
raster. Whether the bytes are *right* is not a readiness question and no
machine here can answer it — dew point in the air-temperature slot passes every
finding below. :data:`~.forms.CHECKS_NOT_MADE` says so on the same page, and
this module does not say it a second time in different words.
"""

from dataclasses import dataclass
from datetime import datetime

from georiva.ingestion.models import RunIngestion

from . import parameters as params
from . import planner
from .models import NOT_A_FORECAST
from .units import same_unit, symbol_of
from .windows import publishable

#: Nothing is in the way of this one.
READY = "ready"

#: Nothing is in the way, and the answer will get better on its own. Only the
#: step count reaches this: a run still ingesting publishes fewer steps now than
#: it will when its stragglers land, and that is a fact worth a number rather
#: than a state worth waiting for.
PARTIAL = "partial"

#: In the way now, gone by itself later. The operator's correct action is to
#: wait, and a surface that told them to change something would be wrong.
WAITING = "waiting"

#: In the way, and only somebody changing something moves it. The operator's
#: correct action is to act, and a surface that told them to wait would cost
#: them the wait.
BLOCKED = "blocked"

#: Not answerable until one of the findings above it is. Distinct from
#: :data:`BLOCKED` on purpose: the step count of a publication with a blank slot
#: is not a second fault, it is the same fault seen from downstream, and two
#: red rows for one cause reads as two things to fix.
UNANSWERABLE = "unanswerable"

#: Worst first. :attr:`Readiness.state` is the first of these any finding holds,
#: which is what makes the page's one-word verdict the same word as its worst
#: row rather than a separate judgment that could disagree with it.
SEVERITY = (BLOCKED, WAITING, UNANSWERABLE, PARTIAL, READY)

#: What a publication can be in and still publish. ``PARTIAL`` is in here and
#: that is the point of its existing: a run mid-ingestion publishes, and
#: publishes again with more steps when the rest lands.
PUBLISHABLE = (READY, PARTIAL)

#: What each state says out loud. Deliberately not the constant: "not ready yet"
#: and "needs a change" are the two sentences an operator acts on, and the
#: vocabulary above is for the code that compares them.
STATE_LABELS = {
    READY: "ready",
    PARTIAL: "still ingesting",
    WAITING: "not ready yet",
    BLOCKED: "needs a change",
    UNANSWERABLE: "cannot say yet",
}

#: The colour each state is emphasised in, in Wagtail's own utility classes and
#: beside its own ``w-status`` pill — the answer :mod:`~.history` and
#: :class:`~.wagtail_hooks.ResidencyColumn` already reached, and for its reason:
#: a plugin has no business shipping admin CSS for what the admin already has.
#: The word carries the state and the colour only finds it, so a state with no
#: rule here renders in the table's ordinary text rather than in another's colour.
STATE_CLASSES = {
    READY: "w-text-positive-100",
    PARTIAL: "w-text-positive-100",
    WAITING: "w-text-grey-400",
    BLOCKED: "w-text-critical-200",
    UNANSWERABLE: "w-text-grey-400",
}

#: The one sentence at the top, per state. The verdict is the worst finding's
#: state and says what to *do* about it, because "not ready yet" and "needs a
#: change" are read as the same bad news by somebody who has not yet found the
#: row that differs.
VERDICTS = {
    READY: "The latest closed run would publish now.",
    PARTIAL: (
        "This publishes now, and the latest run is still ingesting — it would carry fewer "
        "steps than it will once the rest of its files land."
    ),
    WAITING: (
        "Nothing here is misconfigured. This publication is waiting for data, and the next "
        "run to close is what changes that."
    ),
    BLOCKED: (
        "This will not publish until something below is changed. Waiting does not fix any of the rows marked so."
    ),
    UNANSWERABLE: "Not enough is settled yet to say what a publish would do.",
}


@dataclass(frozen=True)
class Finding:
    """One question about this publication, answered."""

    key: str
    subject: str
    state: str
    #: The answer in as few words as it takes — a count, a date, "agree". Read
    #: down the column; :attr:`detail` is read when one of them is surprising.
    answer: str
    detail: str = ""

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def state_class(self) -> str:
        return STATE_CLASSES.get(self.state, "")


@dataclass(frozen=True)
class Readiness:
    """Every finding, and the one word they add up to."""

    findings: tuple[Finding, ...] = ()

    @property
    def state(self) -> str:
        """The worst state any finding holds, which is the whole verdict.

        Derived rather than stored so that the summary and the rows cannot
        disagree: a page saying "ready" over a blocked row is worse than one
        saying nothing at all.
        """
        states = {finding.state for finding in self.findings}
        return next((state for state in SEVERITY if state in states), READY)

    @property
    def can_publish(self) -> bool:
        return self.state in PUBLISHABLE

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def state_class(self) -> str:
        return STATE_CLASSES.get(self.state, "")

    @property
    def verdict(self) -> str:
        return VERDICTS.get(self.state, "")


def report(publication) -> Readiness:
    """What a publish of this publication would meet, in the order it meets it.

    Every figure is read from the database. The publication must have a
    collection — the panel that renders this is hidden on the add form, where
    there is neither.
    """
    collection = publication.collection
    run = RunIngestion.latest_closed(collection)
    mapped = publication.mapped_variables()

    return Readiness(
        findings=(
            _forecast(collection),
            _visibility(collection),
            _runs(collection, run),
            _slots(collection, publication, mapped),
            _units(mapped),
            _steps(collection, run, mapped),
        )
    )


def _forecast(collection) -> Finding:
    """Whether the collection is a forecast at all.

    The chooser offers forecast collections and
    :meth:`~.models.FortiPublication.clean` refuses the rest, so a publication
    arrives here only when the collection was changed *under* it — a tick-box on
    a page in another part of the admin. Nothing downstream says so: the planner
    refuses at the run, which reads as a run that has not closed, and an
    operator would go looking at their ingestion.

    The sentence is the model's own, for the reason the form's unit refusal is
    the model's: there is one explanation of this rule, and a second would be
    free to drift from it.
    """
    if collection.is_forecast:
        return Finding(key="forecast", subject="Forecast collection", state=READY, answer="yes")
    return Finding(
        key="forecast",
        subject="Forecast collection",
        state=BLOCKED,
        answer="no",
        detail=NOT_A_FORECAST,
    )


def _visibility(collection) -> Finding:
    """Whether a reader presenting no credential may read this collection.

    The sixth finding, and the one the ticket did not name. It is here because
    the planner refuses a collection that is not public, and a readiness page
    that reported "ready" over that refusal would have recreated the fault this
    module exists to repair — a publish refused into a build log nothing
    renders — one refusal further down.

    It is not the *publication's* visibility, which is inherited from this and
    may only be narrower. What decides the refusal is the collection's, so that
    is what this row reads.
    """
    if collection.visibility == collection.Visibility.PUBLIC:
        return Finding(key="visibility", subject="Collection visibility", state=READY, answer="public")
    return Finding(
        key="visibility",
        subject="Collection visibility",
        state=BLOCKED,
        answer=collection.visibility,
        detail=(
            f"{collection.slug} is {collection.visibility}, not public. A Forti reader presents "
            f"no credential, so there is nobody to check a restricted collection against and the "
            f"build refuses rather than serving it to anyone who asks."
        ),
    )


def _runs(collection, run) -> Finding:
    """How many closed runs there are, and when the latest was.

    Closed rather than all, because a closed run is what
    :func:`~.planner.plan` reaches for: an open run is still ingesting and has
    no complete forecast to transpose. A collection with open runs and no closed
    one is therefore *waiting*, and saying "1 run" about it would be a true
    sentence answering a question nobody asked.
    """
    closed = RunIngestion.objects.filter(collection=collection, status=RunIngestion.Status.CLOSED).count()
    if run is None:
        return Finding(
            key="runs",
            subject="Closed runs",
            state=WAITING,
            answer="none yet",
            detail=(
                f"{collection.slug} has no closed run. A run closes when its arrival route says "
                f"the last file landed, and a publication configured ahead of its first "
                f"ingestion is an ordinary thing to have — this resolves itself."
            ),
        )
    return Finding(
        key="runs",
        subject="Closed runs",
        state=READY,
        answer=f"{closed}, latest {_stamp(run.reference_time)}",
        detail="",
    )


def _slots(collection, publication, mapped: dict) -> Finding:
    """Whether every slot resolves to a variable this publication can read.

    Two ways one does not, and they are one finding rather than two because they
    are one question — *does this slot resolve?* — and an operator reads the
    answer as one row. The messages are the planner's own reasons in fewer
    words: a **blank** slot is a configuration nobody finished, and a slot
    naming a variable of **another collection** is one that would have no asset
    at any timestep.

    Both are :data:`BLOCKED`. Neither is fixed by any run closing, which is the
    whole of what separates this row from the one above it.
    """
    blank = publication.unmapped_slots(mapped)
    foreign = [
        f"{key} ← {variable.slug}"
        for key, variable in mapped.items()
        if variable is not None and variable.collection_id != collection.pk
    ]
    filled = len(mapped) - len(blank)

    if blank or foreign:
        return Finding(
            key="slots",
            subject="Slot coverage",
            state=BLOCKED,
            answer=f"{filled} of {len(mapped)}",
            detail=_slot_detail(collection, blank, foreign),
        )
    return Finding(
        key="slots",
        subject="Slot coverage",
        state=READY,
        answer=f"{filled} of {len(mapped)}",
    )


def _units(mapped: dict) -> Finding:
    """Whether each filled slot carries the unit that slot publishes.

    Compared through :func:`~.units.same_unit`, which is what the mapping and
    the planner both compare through, so ``°C`` and ``degC`` agree here exactly
    as they agree there and kelvin disagrees in all three places.

    Only the **filled** slots. A blank slot has no unit to disagree, and calling
    that a unit fault would report one cause as two — the row above already says
    the slot is blank.
    """
    filled = {key: variable for key, variable in mapped.items() if variable is not None}
    if not filled:
        return Finding(
            key="units",
            subject="Units",
            state=UNANSWERABLE,
            answer="nothing to compare",
            detail="No slot names a variable yet, so there is no unit to compare with the slot's.",
        )

    wrong = []
    for key, variable in filled.items():
        want = params.BY_SLOT[key].units
        have = symbol_of(variable)
        if not same_unit(have, want):
            wrong.append(f"{key} ← {variable.slug} is {have!r}, expected {want!r}")

    if wrong:
        return Finding(
            key="units",
            subject="Units",
            state=BLOCKED,
            answer=f"{len(wrong)} disagree",
            detail=(
                "Forti reads units from meta.json without converting, so this would publish a "
                "wrong number under a right-looking label: " + "; ".join(wrong) + "."
            ),
        )
    return Finding(key="units", subject="Units", state=READY, answer=f"{len(filled)} agree")


def _steps(collection, run, mapped: dict) -> Finding:
    """How many steps the latest closed run would actually publish.

    The **intersection** across every slot, which is what
    :func:`~.planner.plan` publishes and for its reason: variables of one run
    ingest independently and finish at different step counts, and a step some
    slots have and others do not is a partial forecast Forti cannot express.
    The count is read through the planner's own two functions rather than a
    second query shaped like them, so the number on this page and the number the
    build writes cannot become two numbers.

    Unanswerable rather than blocked when a slot is blank or foreign: there is
    nothing wrong *here*, and the row above already says what is.
    """
    if run is None:
        return Finding(
            key="steps",
            subject="Steps the latest run would publish",
            state=UNANSWERABLE,
            answer="no run to count",
            detail="There is no closed run yet, so there is no step list to intersect.",
        )
    if any(variable is None or variable.collection_id != collection.pk for variable in mapped.values()):
        return Finding(
            key="steps",
            subject="Steps the latest run would publish",
            state=UNANSWERABLE,
            answer="mapping unfinished",
            detail=(
                "A step counts only when every slot has a COG for it, so this cannot be counted "
                "until every slot names a variable of this collection."
            ),
        )

    hrefs = planner.cog_hrefs(collection, run.reference_time, mapped)
    shared = planner.shared_times(hrefs)
    stamp = _stamp(run.reference_time)

    if not shared:
        return Finding(
            key="steps",
            subject="Steps the latest run would publish",
            state=WAITING,
            answer="none yet",
            detail=(
                f"No timestep of {stamp} has a COG for every slot. Variables of one run ingest "
                f"independently, so this resolves itself as they land."
            ),
        )

    if not any(parameter.is_period for parameter in publishable(shared, params.ALL_PARAMETERS)):
        return Finding(
            key="steps",
            subject="Steps the latest run would publish",
            state=WAITING,
            answer=f"{len(shared)}, spanning no window",
            detail=(
                f"{stamp} has {len(shared)} shared step(s) and no two of them are a period window "
                f"apart, so there would be no precipitation and no symbol. Instant values alone "
                f"are not a forecast, and a run with more steps in it spans one."
            ),
        )

    reached = max(len(times) for times in hrefs.values())
    behind = [slot for slot, times in hrefs.items() if len(times) < reached]
    if behind:
        return Finding(
            key="steps",
            subject="Steps the latest run would publish",
            state=PARTIAL,
            answer=f"{len(shared)} of {reached}",
            detail=(
                f"{_list(behind)} has fewer steps of {stamp} than the rest, so a publish now "
                f"carries {len(shared)} of the {reached} steps the run has reached. Nothing needs "
                f"doing: the stragglers land and the next publish carries them."
            ),
        )

    return Finding(
        key="steps",
        subject="Steps the latest run would publish",
        state=READY,
        answer=f"{len(shared)} steps",
        detail="",
    )


def _slot_detail(collection, blank: list, foreign: list) -> str:
    said = []
    if blank:
        said.append(
            f"Nothing is mapped to {_list(blank)}. Every slot has to name a variable of "
            f"{collection.slug} before this can publish — fill the blank rows in below."
        )
    if foreign:
        said.append(
            f"{_list(foreign)} names a variable outside {collection.slug}, which has no asset at "
            f"any timestep of this collection's runs."
        )
    return " ".join(said)


def _list(names) -> str:
    """Names as a sentence reads them, matching :func:`~.mapping._list`."""
    names = list(names)
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _stamp(moment: datetime) -> str:
    """A run's reference time as this plugin writes one everywhere else."""
    return moment.strftime("%Y-%m-%dT%H:%MZ")
