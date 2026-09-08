"""Finding a period parameter's windows in a run's actual time index.

A period parameter — 6-hour precipitation, a 6-hour maximum — is a value *for a
span*, and Forti expresses that span with two numbers that must agree:
``times`` carries the window's **end**, and ``offset`` carries its **length in
hours**, because jsonfrontend files the value under ``end - offset``
(`encode.go:180`).

The trap is that the span must be found by looking up ``t + H`` **in the time
index**, never by taking a fixed number of steps. ECMWF's spacing is 3-hourly to
144 h and 6-hourly beyond, so a 6-hour window is *two steps* early in the run and
*one step* later on. A step-count span is correct for exactly one half of every
run.

This is the mistake M0 actually made. The spike assumed a 6-hourly feed, computed
3-hour windows on a 3-hourly run, labelled them ``next_6_hours``, and jsonfrontend
duly filed them as "the 6 hours starting at …" — wrong length and wrong start,
with nothing anywhere reporting an error. Every layer succeeded.

So windows are pairs of *indices into the run's own time list*, and a window whose
end is not in that list does not exist. That is also what makes a 3-hourly run
publish 6-hour series and no 1-hour ones without anyone configuring it.
"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Window:
    """One span of a period parameter, as indices into the run's time list.

    ``start`` and ``end`` index the time axis; ``end_time`` is what goes into
    ``meta.json``'s ``times``. A derivation reads ``values[start:end + 1]`` — the
    closing window, both endpoints included, which is what an accumulation
    difference and a min/max over the span both need.
    """

    start: int
    end: int
    end_time: object

    @property
    def span(self) -> int:
        """How many steps the window covers. Varies within one run."""
        return self.end - self.start


def find_windows(times, hours: int) -> list[Window]:
    """Every ``hours``-long window whose start *and* end are both in ``times``.

    ``times`` must be sorted ascending. The result is ordered by end time, which
    is the order ``meta.json``'s ``times`` and the packed values must share.

    Returns an empty list when the run's spacing cannot express the window at
    all — a 3-hourly run has no 1-hour window, and that is a fact about the data
    rather than a misconfiguration.
    """
    if hours <= 0:
        raise ValueError(f"A period window must be at least one hour, got {hours}")

    index = {time: position for position, time in enumerate(times)}
    delta = timedelta(hours=hours)

    windows = []
    for position, start_time in enumerate(times):
        end_position = index.get(start_time + delta)
        if end_position is None:
            continue
        windows.append(Window(start=position, end=end_position, end_time=times[end_position]))

    return windows


def publishable(times, parameters) -> list:
    """The subset of ``parameters`` this run's time index can actually express.

    An instant parameter always survives. A period parameter survives only if at
    least one window of its length exists — publishing an empty series would put
    a parameter in ``meta.json`` with no values behind it, and a reader has no
    way to tell that apart from a gap.
    """
    keep = []
    for parameter in parameters:
        if not parameter.is_period:
            keep.append(parameter)
        elif find_windows(times, parameter.offset):
            keep.append(parameter)
    return keep
