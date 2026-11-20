"""Whether a variable's values can be read in a slot's unit — the check Forti cannot make.

``jsonfrontend`` copies units out of ``meta.json`` (`encode.go:88`) without
interpreting them, so nothing downstream of this plugin can tell a celsius value
from a kelvin one labelled celsius. So every slot is read in the unit its
parameter publishes: core's read module converts each value from its Variable's
**source unit** — what the bytes hold (ADR 0046 §1, §11) — whatever unit the
Variable is served in.

A source unit core cannot convert to the slot's unit is a **hard refusal**,
made in three places from this one rule: at the mapping, where a slot is given a
variable; in readiness, before a publish; and at the planner, just before
anything is read.
"""

from georiva.core.unit_utils import Conversion, ConversionRefused, affine_conversion


def conversion_into(variable, unit: str) -> Conversion:
    """How ``variable``'s stored values become values in ``unit``: core's own
    rule (ADR 0046 §4, §6), the one the read applies.

    Raises :class:`ConversionRefused`, with an operator-readable reason, when
    they cannot — another dimension, a non-affine unit, a mass per area that is
    not flagged as water. A variable with no source unit is refused too: core
    requires one, but what its bytes hold cannot be guessed.
    """
    source = getattr(variable, "source_unit", None)
    if source is None:
        raise ConversionRefused(f"{variable.slug!r} does not state what unit its values are stored in.")
    return affine_conversion(source.symbol, unit, water=bool(getattr(variable, "water", False)))


def refusal(variable, unit: str) -> str | None:
    """Why ``variable``'s values cannot be read in ``unit``, or ``None`` when they can."""
    try:
        conversion_into(variable, unit)
    except ConversionRefused as refused:
        return str(refused)
    return None


def source_symbol(variable) -> str | None:
    """The unit ``variable``'s values are stored in, as a symbol, or ``None``."""
    source = getattr(variable, "source_unit", None)
    return source.symbol if source else None
