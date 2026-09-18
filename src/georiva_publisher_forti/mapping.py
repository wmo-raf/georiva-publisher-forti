"""What fills a slot when nobody has said otherwise, and what is odd about it.

Until the mapping became data, which GeoRiva variable fed which Forti parameter
was an exact slug match buried in the planner: a collection had to call its
2-metre temperature ``2t`` or it could not publish at all. That rule is not
wrong — it is right for every collection this plugin has ever published — so it
survives as the **default**, and what changed is that it is now overridable.

Beside it sits the other judgment a mapping admits of, and it is a weaker one.
:func:`concerns` is what the editor warns with: two mappings that are perfectly
legal and are also what a mistake looks like. Neither is a refusal, because a
refusal would be a lie — a variable may deliberately fill two slots, and a
declared range is a styling hint rather than a measurement. And neither sees the
confusion that matters: dew point mapped into the air-temperature slot is
degrees celsius into degrees celsius, every layer downstream agrees, and no
machine check will ever catch it. A surface that renders these without saying so
has told an operator their mapping was verified. It was not.

Kept in a module of its own, pure, and importing nothing from Django, because a
data migration imports it. A migration that reached into ``models`` would
rewrite its own history the next time a model changed; one that duplicated the
rule would describe the bytes a publication is serving using a rule that is no
longer the one that produced them. :func:`concerns` is written to the same
constraint for a different reason: it is a judgment about eight names and eight
number pairs, and a function that needed a database to make it could not be
tested without one.
"""

from dataclasses import dataclass

from . import parameters as params
from .prose import listed

#: A variable that also fills another slot. The same series is then published
#: twice under two names — deliberate for a collection that genuinely has one
#: field where the format expects two, and otherwise a slot filled from the row
#: above by mistake.
SHARED = "shared"

#: A declared range that shares no value with the one the slot publishes. See
#: :data:`~.parameters.PLAUSIBLE_RANGES` for why this is evidence and not proof.
RANGE = "range"


@dataclass(frozen=True)
class Concern:
    """One doubt about one slot, raised and not enforced."""

    slot: str
    kind: str
    message: str


def auto_match(variables) -> dict:
    """``{slot key: variable or None}`` — every slot answered, by slug.

    Takes anything with a ``.slug``, so a historical model in a migration and a
    real ``Variable`` both work. Every slot appears in the result whether or not
    a variable fills it: a blank slot is a thing a publication *has*, and being
    asked to fill one in is how a collection that names its variables
    differently becomes publishable at all.
    """
    by_slug = {variable.slug: variable for variable in variables}
    return {key: by_slug.get(key) for key in params.SLOT_KEYS}


def concerns(mapped: dict) -> tuple[Concern, ...]:
    """Everything suspicious about a mapping that is nonetheless saveable.

    ``mapped`` is :meth:`~.models.FortiPublication.mapped_variables`' shape —
    every slot, filled or not — or the same shape assembled from a form that has
    not been saved, which is the case the editor actually needs.

    In vocabulary order, which is the order the editor renders its rows in, so a
    list of warnings and the rows it is about read down the page together. Both
    concerns about one slot come back as two entries rather than one merged
    sentence: they are separate claims about separate evidence, and an operator
    who dismisses one has not thereby dismissed the other.

    Variables are told apart by **slug**, not by identity: every variable in a
    mapping belongs to the same collection — the model refuses one that does
    not — and a slug is unique within a collection. That is also what lets this
    stay free of the database, and what lets an unsaved form be checked before
    anything is written rather than after.
    """
    raised = []
    for key in params.SLOT_KEYS:
        variable = mapped.get(key)
        if variable is None:
            continue
        slot = params.BY_SLOT[key]
        shared = _filled_by(mapped, variable)
        if len(shared) > 1:
            raised.append(Concern(slot=key, kind=SHARED, message=_shared_message(variable, shared)))
        if _range_is_unreachable(slot, variable):
            raised.append(Concern(slot=key, kind=RANGE, message=_range_message(slot, variable)))
    return tuple(raised)


def _filled_by(mapped: dict, variable) -> tuple[str, ...]:
    """Every slot this variable fills, in vocabulary order."""
    return tuple(key for key in params.SLOT_KEYS if mapped.get(key) is not None and mapped[key].slug == variable.slug)


def _shared_message(variable, shared: tuple[str, ...]) -> str:
    """One sentence about the pair, shown on both of its rows.

    Symmetric on purpose: the concern is not that *this* slot is shared, it is
    that these slots are the same series, and an operator reading either row
    should learn the whole of it without having to find the other.
    """
    published = " and as ".join(listed(params.BY_SLOT[key].feeds) for key in shared)
    return (
        f"{variable.slug!r} is used for both {listed(shared)}, so the same data would be "
        f"published as {published}. Check this is what you mean."
    )


def _range_is_unreachable(slot, variable) -> bool:
    """Whether the variable's declared range reaches the slot's at all.

    Overlap rather than containment, and the difference is what keeps this worth
    reading. A generous styling range is ordinary — so is core's untuned 0–1
    default (ADR 0022), which most variables carry until somebody opens the
    Styling page — and warning on either would make the warning noise. A range
    that shares no value with the slot's is a different claim: whatever this
    variable holds, the slot cannot publish it.
    """
    low, high = slot.plausible
    declared_low = getattr(variable, "value_min", None)
    declared_high = getattr(variable, "value_max", None)
    if declared_low is None or declared_high is None:
        return False
    return declared_high < low or declared_low > high


def _range_message(slot, variable) -> str:
    low, high = slot.plausible
    return (
        f"{variable.slug!r} has a value range of {_number(variable.value_min)} to "
        f"{_number(variable.value_max)}, but {slot.key} is normally {_number(low)} to "
        f"{_number(high)} {slot.units}. The numbers may be in a different unit than the label says."
    )


def _number(value: float) -> str:
    """A bound as a person writes it: no trailing zero on a whole number, and no
    thousands separator on a figure that is a range endpoint rather than a
    quantity anybody adds up."""
    return f"{value:g}"
