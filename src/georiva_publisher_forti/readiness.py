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

## One finding can silence another

The step count is the only finding that describes a publish rather than a fact,
and it says nothing at all while anything above it is blocked. ``plan`` refuses
at the collection, the mapping or the units *before* it reaches the step
intersection, so a count rendered beside such a row would promise a publish that
is not going to happen — and a green row beside a red one reads as the part that
is fine.

## Where the rules come from

Every rule here is the planner's, reached through the planner:
:func:`~.planner.readable_without_credentials`, :func:`~.planner.cog_hrefs`,
:func:`~.planner.shared_times`, :func:`~.planner.spans_a_period`, and
:data:`~.models.NOT_A_FORECAST` and :data:`~.planner.NOT_PUBLIC` for the two
sentences a refusal and a finding have to say identically. What is written here
is the *prose a form needs* — "this resolves itself", "fill the blank rows in
below" — which an exception raised into a build log has no use for. Rules
shared, wording per audience: the reverse arrangement is the one where the form
promises a number the build disagrees with.

## What it does not ask

It reads the database and nothing else: no bucket, no status document, no
raster. Whether the bytes are *right* is not a readiness question and no
machine here can answer it — dew point in the air-temperature slot passes every
finding below. :data:`~.forms.CHECKS_NOT_MADE` says so on the same page, and
this module does not say it a second time in different words.
"""

from dataclasses import dataclass

from georiva.ingestion.models import RunIngestion

from . import parameters as params
from . import planner
from .models import NOT_A_FORECAST
from .prose import listed, stamp
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

#: What each state says out loud. Deliberately not the constant: "not ready yet"
#: and "needs a change" are the two sentences an operator acts on, and the
#: vocabulary above is for the code that compares them.
STATE_LABELS = {
    READY: "ready",
    PARTIAL: "still arriving",
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
    # Warning rather than positive, which is how :mod:`~.verification` already
    # colours a state that is neither good nor bad: "still ingesting" publishes,
    # and an operator who reads it as "ready" has been told the run is finished.
    PARTIAL: "w-text-warning-100",
    WAITING: "w-text-grey-400",
    BLOCKED: "w-text-critical-200",
    UNANSWERABLE: "w-text-grey-400",
}

#: The one sentence at the top, per state. The verdict is the worst finding's
#: state and says what to *do* about it, because "not ready yet" and "needs a
#: change" are read as the same bad news by somebody who has not yet found the
#: row that differs.
VERDICTS = {
    READY: "The latest forecast run is ready to publish.",
    PARTIAL: "Ready to publish. The latest run is still arriving, so more steps will follow.",
    WAITING: "Nothing needs changing. Waiting for the next forecast run to arrive.",
    BLOCKED: "Something below needs a change before this can publish. Waiting will not fix it.",
    UNANSWERABLE: "Cannot say yet — see the rows above.",
}


#: The six questions, in the order an operator meets them: the key a caller names
#: a finding by, and the words the page heads its row with. One table rather than
#: a pair of arguments at each construction, so that the key a test asks for and
#: the subject a reader sees cannot come apart.
SUBJECTS = {
    "forecast": "Is a forecast collection",
    "visibility": "Collection is public",
    "runs": "Forecast runs received",
    "slots": "Variables set",
    "units": "Units match",
    "steps": "Steps ready to publish",
}


class _Stated:
    """The two words a state is rendered as, for whatever holds one.

    On a mixin rather than written on both dataclasses below, which is the
    duplication it replaces: a finding's state and the section's state are the
    same vocabulary, and a rule added to one and not the other would colour a row
    differently from the summary that is derived from it.
    """

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def state_class(self) -> str:
        return STATE_CLASSES.get(self.state, "")


@dataclass(frozen=True)
class Finding(_Stated):
    """One question about this publication, answered."""

    key: str
    state: str
    #: The answer in as few words as it takes — a count, a date, "agree". Read
    #: down the column; :attr:`detail` is read when one of them is surprising.
    answer: str
    detail: str = ""

    @property
    def subject(self) -> str:
        return SUBJECTS[self.key]


@dataclass(frozen=True)
class Readiness(_Stated):
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
    def verdict(self) -> str:
        return VERDICTS.get(self.state, "")


def report(publication) -> Readiness:
    """What a publish of this publication would meet, in the order it meets it.

    Every figure is read from the database. The publication must have a
    collection — the inspect page that renders this only exists for a saved
    publication, which always has one.
    """
    collection = publication.collection
    run = RunIngestion.latest_closed(collection)
    mapped = publication.mapped_variables()
    blank, foreign = _unresolved_slots(collection, publication, mapped)

    above = (
        _forecast(collection),
        _visibility(collection),
        _runs(collection, run),
        _slots(collection, mapped, blank, foreign),
        _units(mapped),
    )
    return Readiness(findings=(*above, _steps(collection, run, mapped, refused=_any_blocked(above))))


def _any_blocked(findings) -> bool:
    """Whether a publish would be refused before it counted a single step.

    Every :data:`BLOCKED` finding above the step count is one ``planner.plan``
    raises on *before* it reaches the intersection — the collection, the
    mapping, the units — so a step count rendered beside one would be a promise
    about a publish that is not going to happen. Asked of the findings rather
    than by re-testing their conditions, which is what keeps the two from
    disagreeing when a finding is added.
    """
    return any(finding.state == BLOCKED for finding in findings)


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
        return Finding(key="forecast", state=READY, answer="yes")
    return Finding(
        key="forecast",
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
    if planner.readable_without_credentials(collection):
        return Finding(key="visibility", state=READY, answer="public")
    return Finding(
        key="visibility",
        state=BLOCKED,
        answer=collection.visibility,
        detail=planner.NOT_PUBLIC.format(slug=collection.slug, visibility=collection.visibility),
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
            state=WAITING,
            answer="none yet",
            detail=f"No complete forecast run of {collection.slug} has arrived yet. This resolves itself.",
        )
    return Finding(
        key="runs",
        state=READY,
        answer=f"{closed}, latest {stamp(run.reference_time)}",
        detail="",
    )


def _unresolved_slots(collection, publication, mapped: dict) -> tuple[list, list]:
    """The slots that do not resolve, in the two ways one can fail to.

    Worked out once and read twice — by the finding that reports it and by the
    step count, which cannot be counted without it — rather than by two
    comprehensions that would have to be kept saying the same thing.

    **Blank** is asked of the publication, which already answers it. **Foreign**
    is the planner's other refusal: a variable of another collection has no
    asset at any timestep of this one's runs.
    """
    blank = publication.unmapped_slots(mapped)
    foreign = [
        f"{key} ← {variable.slug}"
        for key, variable in mapped.items()
        if variable is not None and variable.collection_id != collection.pk
    ]
    return blank, foreign


def _slots(collection, mapped: dict, blank: list, foreign: list) -> Finding:
    """Whether every slot resolves to a variable this publication can read.

    Both ways of failing are one finding rather than two, because they are one
    question — *does this slot resolve?* — and an operator reads the answer as
    one row.

    Both are :data:`BLOCKED`. Neither is fixed by any run closing, which is the
    whole of what separates this row from the one above it.
    """
    filled = len(mapped) - len(blank)
    answer = f"{filled} of {len(mapped)}"
    if blank or foreign:
        return Finding(key="slots", state=BLOCKED, answer=answer, detail=_slot_detail(collection, blank, foreign))
    return Finding(key="slots", state=READY, answer=answer)


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
            state=UNANSWERABLE,
            answer="nothing to compare",
            detail="No variables are set yet.",
        )

    wrong = []
    for key, variable in filled.items():
        want = params.BY_SLOT[key].units
        have = symbol_of(variable)
        if not same_unit(have, want):
            wrong.append(f"{variable.slug} is in {have}, but {key} must be in {want}")

    if wrong:
        return Finding(
            key="units",
            state=BLOCKED,
            answer=f"{len(wrong)} disagree",
            detail="Units are not converted, so the numbers would be wrong: " + "; ".join(wrong) + ".",
        )
    return Finding(key="units", state=READY, answer=f"{len(filled)} agree")


def _steps(collection, run, mapped: dict, refused: bool) -> Finding:
    """How many steps the latest closed run would actually publish.

    The **intersection** across every slot, which is what
    :func:`~.planner.plan` publishes and for its reason: variables of one run
    ingest independently and finish at different step counts, and a step some
    slots have and others do not is a partial forecast Forti cannot express.
    The count is read through the planner's own functions rather than a second
    query and a second rule shaped like them, so the number on this page and the
    number the build writes cannot become two numbers.

    **It answers nothing while anything above is blocked.** A publish refused at
    the collection, the mapping or the units never reaches the intersection, so a
    count rendered beside such a row would be a promise about a publish that is
    not going to happen — and a green row beside a red one reads as the part that
    is fine. Unanswerable rather than blocked, because there is nothing wrong
    *here*: the row above already says what is.
    """
    if refused:
        return Finding(
            key="steps",
            state=UNANSWERABLE,
            answer="not until the rows above",
            detail="Fix the rows above first.",
        )
    if run is None:
        return Finding(
            key="steps",
            state=UNANSWERABLE,
            answer="no run to count",
            detail="No forecast run has arrived yet.",
        )

    hrefs = planner.cog_hrefs(collection, run.reference_time, mapped)
    shared = planner.shared_times(hrefs)
    when = stamp(run.reference_time)

    if not shared:
        return Finding(
            key="steps",
            state=WAITING,
            answer="none yet",
            detail=f"No step of the {when} run has data for every variable yet. This resolves itself as data arrives.",
        )

    if not planner.spans_a_period(publishable(shared, params.ALL_PARAMETERS)):
        return Finding(
            key="steps",
            state=WAITING,
            answer=f"{len(shared)}, too close together",
            detail=(
                f"The {when} run has {len(shared)} step(s) so far, too close together to cover a "
                f"period — there would be no rainfall and no weather symbols yet. More steps will fix it."
            ),
        )

    reached = max(len(times) for times in hrefs.values())
    behind = [slot for slot, times in hrefs.items() if len(times) < reached]
    if behind:
        return Finding(
            key="steps",
            state=PARTIAL,
            answer=f"{len(shared)} of {reached}",
            detail=(
                f"{listed(behind)} has fewer steps of the {when} run than the other variables, so "
                f"a publish now would carry {len(shared)} of {reached} steps. Nothing needs doing."
            ),
        )

    return Finding(key="steps", state=READY, answer=f"{len(shared)} steps")


def _slot_detail(collection, blank: list, foreign: list) -> str:
    said = []
    if blank:
        said.append(
            f"{listed(blank)} is not set. Every Forti parameter needs a variable — set it on the Variables page."
        )
    if foreign:
        said.append(f"{listed(foreign)} uses a variable from a different collection than {collection.slug}.")
    return " ".join(said)
