"""The config that turns Forti's bytes back into locationforecast 2.0.

``jsonfrontend`` reads one config describing which internal parameter belongs in
which time bucket and under what name. It is **org-level**, not per-area — one
``jsonfrontend`` fronts one organisation's whole prefix — so it is the union over
that organisation's enabled publications: a coarse global area and a finer
national one blend by nearest gridpoint, and a consumer sees one document either
way.

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
    """The union config for one organisation's enabled publications.

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


def publish(organisation, publications) -> str | None:
    """Write the organisation's ``jsonformat.json``. Returns the key, or None.

    Not a completion marker: nothing loads on its appearance, and jsonfrontend
    reads it once at startup. It lives at the root of the org's prefix because it
    describes the whole prefix rather than any one area.
    """
    publications = [publication for publication in publications if publication.is_enabled]
    if not publications:
        return None

    from georiva.core.publishing import PublicationSink

    from .models import MARKER_PATTERNS, SINK_SLUG

    sink = PublicationSink(organisation.slug, SINK_SLUG, marker_patterns=MARKER_PATTERNS)
    return sink.write_json("jsonformat.json", build(publications))
