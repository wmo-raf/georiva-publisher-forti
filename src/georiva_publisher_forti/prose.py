"""How a list and a moment are written where a person reads them.

Two formattings that were each written twice — once in :mod:`mapping` and once
in :mod:`readiness`, once in :mod:`planner`'s refusals and once in the findings
that report them — and that have to agree, because the same run and the same
three slots are named on two surfaces an operator reads together. A reference
time spelled two ways reads as two runs.

Pure, and importing nothing but :mod:`datetime`, because :mod:`mapping` imports
it and a data migration imports :mod:`mapping`. That constraint is argued once,
in that module's docstring; this one only has to stay inside it.
"""

from datetime import datetime


def listed(names) -> str:
    """Names as a sentence reads them.

    So that a message about one slot does not say "1 slot" and a message about
    three does not run them together.
    """
    names = list(names)
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def stamp(moment: datetime) -> str:
    """A run's reference time, in the one spelling this plugin uses for it."""
    return moment.strftime("%Y-%m-%dT%H:%MZ")
