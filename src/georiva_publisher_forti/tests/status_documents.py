"""The status documents the Go side writes, written by a test instead.

One spelling, because two test modules read the same document for two different
surfaces — the panel's four hops and the listing's one cell — and a payload
shape typed out twice is one that drifts in one of the two. The drift would be
invisible in the direction that matters: a key this reader ignores can be
misspelled in a fixture forever, and the test that would have caught it is the
one testing the other surface.

Deliberately not in :mod:`factories`, which builds rows and rasters. This builds
bytes on a bucket, which is a different kind of fixture and the only kind these
two modules share.
"""

import json

from georiva_publisher_forti import models, verification

#: What ``configwatch`` reports when the document it read was accepted. A
#: fixture's sha only has to be a sha — the hop comparison is against what
#: :mod:`config` intends, and a test that cares passes its own.
ACCEPTED_SHA = "f" * 64


def write_status(path, payload):
    """Through ``models.instance_sink`` by attribute, never by imported name.

    :class:`~.sink_isolation.TemporarySinkMixin` isolates a test by **rebinding**
    ``models.instance_sink``, which cannot reach a name another module has
    already imported. A ``from … import instance_sink`` here would therefore
    write through the real publications bucket while the code under test read
    from the temporary one — the test failing for a reason that looks like the
    feature being broken, and a stray status document left on a real bucket. That
    has happened here once already; see :mod:`.sink_isolation`.
    """
    models.instance_sink().write(path, json.dumps(payload).encode("utf-8"))


def write_forecaster(loaded_sha=ACCEPTED_SHA, *, areas=None, store_error=None, ok=True, errors=None):
    """``status/rawdataforecaster.json``, as the sidecar would have uploaded it.

    ``areas=None`` means *the reader reports an empty list*, not *the reader
    reports no state*: the second is a whole distinction of its own
    (:data:`~.verification.UNREPORTED`) and is asked for by writing the document
    without a ``state`` through :func:`write_status`, so that it cannot be
    reached by accident from here.
    """
    state = {"areas": areas if areas is not None else []}
    if store_error:
        state["store_error"] = store_error

    payload = {
        "module": verification.RAWDATAFORECASTER_MODULE,
        "loaded_sha": loaded_sha,
        "loaded_at": "2026-09-13T09:00:10Z",
        "ok": ok,
        "state": state,
    }
    if errors:
        payload["errors"] = errors

    write_status(verification.RAWDATAFORECASTER_STATUS_PATH, payload)
