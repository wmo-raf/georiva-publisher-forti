"""The config that turns Forti's bytes back into locationforecast 2.0.

``jsonfrontend`` reads one config describing which internal parameter belongs in
which time bucket and under what name. It is **instance-wide** (D20), because one
``jsonfrontend`` now fronts one prefix holding every organisation's areas — so it
is the union over every enabled publication on the instance.

It *watches* that config rather than reading it once — a ``configwatch.Watcher``
polling every second, validating before applying and keeping the previous config
when handed a bad one — so a rewrite needs no restart. Where the document lives,
when it is written and the refusal to write an empty one all belong to
:mod:`config`; this module builds it and does nothing else with it.

A union is safe across tenants for the same reason it was already safe across
areas: the name → bucket mapping is a static function of the parameter *name* and
not of who published it, and jsonfrontend omits an empty duration bucket entirely
(`encode.go:184`) — a 3-hourly model returns no ``next_1_hours`` while an hourly
one does, from one document. What it genuinely costs is the three settings below:
``cut_forecast``, ``data_expiry_offset`` and ``skip_altitude`` are now global, so
an hourly model and a 12-hourly one share one expiry hint.

Two settings copied from met.no's production config rather than the README:

- ``cut_forecast`` drops timesteps before the current hour, which is what makes a
  six-hour-old run answer sensibly instead of leading with the past.
- ``data_expiry_offset`` is **1800**, not the 180 this plan first pencilled in.

Units need no config at all: ``getMeta`` (`encode.go:88`) reads them from
``meta.json``.
"""

from . import parameters as params

#: Seconds after a run's last timestep at which a response stops being served.
DATA_EXPIRY_OFFSET = 1800


def build(publications) -> dict:
    """The union config for every enabled publication on the instance.

    A parameter appears if *any* area publishes it, because jsonfrontend resolves
    per point against whichever area answered — an area that lacks a parameter
    simply contributes no value for it, which is what a partially-covered
    consumer already expects.
    """
    published = set()
    for publication in publications:
        published.update(publication.published_parameters or [])

    groups = {}
    for name in sorted(published):
        parameter = params.BY_NAME.get(name)
        if parameter is None:
            # A name published by an older version of this plugin. Leaving it out
            # is right: jsonfrontend would look for a parameter we no longer know
            # how to describe.
            continue

        group = groups.setdefault(
            parameter.group,
            {"offset": parameter.offset, "parameters": {}},
        )
        if parameter.summary:
            group.setdefault("summary", {})[parameter.locationforecast] = parameter.name
        else:
            group["parameters"][parameter.name] = parameter.locationforecast

    # A group with neither values nor a summary would be an empty duration bucket.
    groups = {name: group for name, group in groups.items() if group["parameters"] or group.get("summary")}

    return {
        "parameters": groups,
        "http_headers": [{"key": "Access-Control-Allow-Origin", "value": "*"}],
        "offer_gzip": True,
        # No DEM is wired up yet, so there is no altitude correction to apply and
        # claiming one would be a lie about the Rift Valley in particular.
        "skip_altitude": True,
        "cut_forecast": True,
        "data_expiry_offset": DATA_EXPIRY_OFFSET,
    }
