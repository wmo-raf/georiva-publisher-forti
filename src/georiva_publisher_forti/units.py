"""Whether two unit symbols name the same unit — the check Forti cannot make.

``jsonfrontend`` copies units out of ``meta.json`` (`encode.go:88`) without
interpreting them, so nothing downstream of this plugin can tell a celsius value
from a kelvin one labelled celsius. The check has to happen here or nowhere, and
it is a **hard refusal** in both places it is made: at the mapping, where a slot
is given a variable, and at the planner, just before anything is read.
"""


def same_unit(symbol: str | None, expected: str) -> bool:
    """Whether two symbols name the same unit, not merely a compatible one.

    Compared through pint rather than as strings: an instance may spell degrees
    Celsius ``°C`` or ``degC`` depending on which seed wrote the row, and both
    are the same unit. Kelvin is *compatible* with Celsius and is not the same
    unit — which is the whole point of checking, since Forti converts nothing.

    A symbol pint cannot parse falls back to an exact string match. That is
    stricter than necessary and refuses rather than guesses, which is the right
    way round when the alternative is publishing a wrong number.
    """
    if symbol is None:
        return False
    if symbol == expected:
        return True

    from georiva.core.models.units import ureg

    try:
        return str(ureg(symbol).u) == str(ureg(expected).u)
    except Exception:
        return False


def symbol_of(variable) -> str | None:
    """The unit symbol a variable carries, or ``None``.

    Core declares ``Variable.unit`` non-nullable, so the guard is defensive
    rather than expected — and it is here once rather than at each of the three
    sites that compare a variable against a slot, none of which wants to be the
    place that decided what a variable with no unit means.
    """
    unit = variable.unit if variable else None
    return unit.symbol if unit else None
