"""The config that tells ``rawdataforecaster`` which areas to load.

Unlike ``jsonformat.json`` this file is **not** run-dependent: it names areas, a
bucket and a distance, none of which change when a run lands. It changes when
the instance's *publication set* changes, which is an operator event.

It is published to the bucket under ``_forti/config/``, beside
``jsonformat.json``, and pulled down by the ``mc`` sidecar (D21). An earlier
draft generated it onto disk beside a compose fragment and said that also
avoided a bootstrap knot, since a config fetched from the bucket would be the
thing that names the bucket. **The knot is not real.** The sidecar sets its own
alias from its own environment — endpoint, access key, secret — so it reaches
the bucket without reading anything out of it, and what the config names is
where *rawdataforecaster* reads forecasts from, which is a different question
from where the sidecar fetches config. What generating onto disk actually cost
was a bind mount from the repository into the container, which is exactly what
D21 removes and what makes D19 — core knowing nothing about Forti — reachable.

Both config documents therefore travel the same route: down through the bucket,
into the config volume, watched in place. An earlier draft had them travelling
differently because ``rawdataforecaster`` did not watch its config while
``jsonfrontend`` did. It does now: ``ColdChanges``
(`rawdataforecaster/internal/server/config/reload.go`) names the settings that
need a restart and deliberately leaves the area list out of them, because adding
or removing an area takes effect within one poll. Which is the reason M5.5 can
rewrite this file on every publish at all.

Two properties of the reader shape everything here.

**A configured area with no data is fatal.** ``forecast.New`` calls ``update()``
synchronously and returns its error, and ``main.go:38`` is
``log.Fatalln(server.Run(...))``. A missing ``latest/<area key>`` used to read as
version 0, so the process looked for ``<area key>/0/complete.json``, did not find
it, and exited. M5.1 fixed that in the fork — a marker miss is now told from a
version 0 and skipped — but :func:`servable` stays, and stays first: it is the
belt to that fix's braces, and it is the half that runs against an image nobody
has rebuilt yet.

**``max_size_gib`` is an object, not a number.** ``dataset.go:50`` does an
unchecked ``maxSizes.(map[string]interface{})`` and the README's bare number
panics the process. Write ``{"<area key>": N}``.
"""

import math

#: Metres. Beyond this the reader answers "outside coverage" instead of the
#: nearest point it happens to hold. Load-bearing rather than a tuning knob:
#: with one area loaded, ``bestArea`` (`forecast.go:92`) resolves *every*
#: coordinate on Earth to the nearest Kenya gridpoint, and this is the only
#: thing that stops it. Measured against the Kenya bbox by M5 and settled at
#: 50 km (§10) — safe against the ≈19.7 km interior worst case and buying ~1.8
#: cells beyond a bbox edge.
#:
#: It lives here and not in core settings. It is a forti-shaped fact — one
#: reader's coverage rule — and core knows nothing about Forti (D19). It is also
#: process-wide and cold: one value for every model on the instance, changed
#: only by a restart, which is one of the things D14 explicitly gives up.
DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE = 50000

#: Bytes per value in the packed record: one int16.
_BYTES_PER_VALUE = 2

#: The memory loader holds a whole area resident. Areas are small — a full Kenya
#: run is ~2.7 MB — so the floor matters more than the estimate.
MINIMUM_MAX_SIZE_GIB = 1


def servable(publications):
    """The publications an area list may safely name.

    Enabled is not enough. ``published_version`` is set by the engine only after
    ``latest/<area key>`` has been written, so it is the one field that answers
    "does this area exist on the bucket" without a round trip.

    Which is why the refresh has to run after ``mark_ready`` and not merely
    after the markers: between those two writes the bytes exist and this field
    does not, and a config built there omits the very run that triggered it.
    """
    return [
        publication
        for publication in publications
        if publication.is_enabled and publication.published_version is not None
    ]


def max_size_gib(publication) -> int:
    """The memory ceiling for one area, in whole GiB.

    Derived from what was last published rather than configured, because the
    number that matters is the resident size of the grid the reader will load.
    Rounded up and floored at 1: the loader compares against this before
    downloading, so an under-estimate refuses to load an area that would have fit.
    """
    values = (publication.point_count or 0) * max(publication.published_step_count or 0, 1)
    gib = (values * _BYTES_PER_VALUE) / (1024**3)
    return max(MINIMUM_MAX_SIZE_GIB, math.ceil(gib))


def source_bucket_url(bucket: str, endpoint: str, prefix: str, region: str = "us-east-1") -> str:
    """The gocloud URL for the instance's Forti prefix.

    ``?prefix=`` is handled generically at the URLMux level (`blob/blob.go:1556`
    → ``PrefixedBucket``), so the ``_forti/`` grammar survives into every driver.

    It bounds the reader to Forti's own corner of the publications bucket and
    **nothing between tenants**. That was D3's claim — one prefix per
    organisation, and the prefix is the whole of the tenant isolation — and
    D14/D15 superseded it: there is one prefix for the instance, so ``Latest()``
    (`client.go:82`) lists and parses every organisation's markers on every poll,
    by design. What separates tenants now is the area key in the request and the
    ``meta.area`` equality check on the response, neither of which is a storage
    fact.
    """
    host = endpoint.split("://", 1)[-1].rstrip("/")
    insecure = not endpoint.startswith("https://")
    return (
        f"s3://{bucket}"
        f"?endpoint=http{'' if insecure else 's'}://{host}"
        f"&region={region}"
        "&use_path_style=true"
        f"&disable_https={'true' if insecure else 'false'}"
        f"&prefix={prefix}"
    )


def build(
    publications,
    *,
    bucket: str,
    endpoint: str,
    prefix: str,
    region: str = "us-east-1",
    maximum_gridpoint_distance: int = DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE,
) -> dict:
    """The instance's ``rawdataforecaster.json``.

    Every published area on the instance, not one organisation's — so one
    organisation's first publish rewrites the file that governs every
    organisation's serving. That is safe only because it comes from one query
    with one test and never from per-org string assembly: a malformed file is
    caught by configwatch and the previous config kept, but a *valid* file with
    an area missing takes that area off the air with nothing to say so.

    ``publications`` is filtered through :func:`servable` here rather than by the
    caller, so there is one place that can get it wrong.
    """
    areas = servable(publications)
    return {
        "source": {"bucket": source_bucket_url(bucket, endpoint, prefix, region)},
        "areas": sorted(publication.area_key for publication in areas),
        "loader": {
            "type": "memory",
            # An object keyed by area key. The documented bare number panics.
            "configuration": {
                "max_size_gib": {publication.area_key: max_size_gib(publication) for publication in areas}
            },
        },
        "maximum_gridpoint_distance": maximum_gridpoint_distance,
    }
