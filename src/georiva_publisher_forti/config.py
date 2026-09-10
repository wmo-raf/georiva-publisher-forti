"""The two config documents the serving pair reads, and the one rule they share.

``rawdataforecaster`` is told which areas to load; ``jsonfrontend`` is told how
to turn the bytes back into locationforecast 2.0. They are different documents
for different processes, and they are written together because they are two
projections of one query — *what this instance is currently serving* — and
writing them apart is how they come to disagree.

Both live under ``_forti/config/`` and travel by the same route: written here,
pulled down by the ``mc`` sidecar, watched in place by the process that reads
them (D21). Neither is a completion marker: nothing loads on their appearance,
and the markers under ``latest/`` are what make a version real. The ordering
between them is the other way round and it matters — **config last, always after
the markers** — because this file advertises a parameter or an area and the
markers are what make its bytes exist. Advertising first is silent in both
directions: an area listed before its first marker is a load failure, and a
marker written before the area is listed is data nothing asks for.

**A document that would be rejected is not written.** Both processes validate the
same way and fail the same way, and it is worth spelling out because the failure
is deferred and therefore hard to attribute:

- ``jsonfrontend``: an empty ``parameters`` map is fatal (`config.go:75`).
- ``rawdataforecaster``: an empty ``areas`` list is fatal
  (`config/reload.go`, "every request would answer no datasets available").

In both, ``configwatch`` validates before applying and keeps the previous config
when handed a bad one — so a running process shrugs it off — while
``watcher.Load()`` at startup takes the same rejection straight to
``log.Fatalf``. The damage is therefore invisible until the next restart, which
is the worst possible moment to discover it. Both empty states are ordinary:
a freshly created publication that has not published yet, or the last published
one being disabled. In every such case the right document is the one already
there, so the refusal is to write rather than to write an empty one.

The encoding is this module's, not core's. ``PublicationSink.write_json`` would
serve, but the sha of these bytes is the thing M5.8's panel compares across four
hops — intended, bucket, volume, loaded — so the bytes have to be ours to
promise, and a change in core's private encoder must not read here as a document
that has changed.
"""

import hashlib
import json
import logging

from django.conf import settings

from . import jsonformat, rdfconfig

logger = logging.getLogger(__name__)

#: Where the sidecar looks. One directory, one source, both documents (D21).
#: §6's diagram put ``jsonformat.json`` at the root of the prefix and only
#: ``rawdataforecaster.json`` under ``config/``; the diagram predates D21, which
#: makes this directory the sidecar's single mount point, and a document outside
#: it is a second thing to remember to copy.
CONFIG_PREFIX = "config"

JSONFORMAT_PATH = f"{CONFIG_PREFIX}/jsonformat.json"
RAWDATAFORECASTER_PATH = f"{CONFIG_PREFIX}/rawdataforecaster.json"


def encode(document: dict) -> bytes:
    """The file's bytes: stable between regenerations, and byte-identical for an
    unchanged document so the sha comparison means what it says."""
    return json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def storage_settings() -> dict:
    """Where the reader is pointed, out of core's *generic* storage settings.

    ``GEORIVA_PUBLICATIONS_BUCKET``, ``AWS_S3_ENDPOINT_URL`` and
    ``AWS_S3_REGION_NAME`` describe the instance's object storage and say nothing
    about Forti, so they stay in core. The one forti-shaped number —
    ``maximum_gridpoint_distance`` — is this plugin's, with its default in
    :mod:`rdfconfig` and an override read here (D19).
    """
    bucket = getattr(settings, "GEORIVA_PUBLICATIONS_BUCKET", None)
    if not bucket:
        raise RuntimeError("GEORIVA_PUBLICATIONS_BUCKET is not set; there is no bucket to name.")

    endpoint = getattr(settings, "AWS_S3_ENDPOINT_URL", "") or ""
    if not endpoint:
        raise RuntimeError("AWS_S3_ENDPOINT_URL is not set; the config would name no endpoint.")

    return {
        "bucket": bucket,
        "endpoint": endpoint,
        "region": getattr(settings, "AWS_S3_REGION_NAME", "us-east-1") or "us-east-1",
        "maximum_gridpoint_distance": getattr(
            settings,
            "GEORIVA_FORTI_MAXIMUM_GRIDPOINT_DISTANCE",
            rdfconfig.DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE,
        ),
    }


def documents(publications, *, bucket, endpoint, region="us-east-1", maximum_gridpoint_distance=None) -> dict:
    """Both documents from one list of publications, keyed by path under the root.

    A document either belongs in the answer or is absent from it — see the module
    docstring for why an empty one is worse than a stale one. Absent means "leave
    what is on the bucket alone", never "delete it".
    """
    from .models import SINK_ROOT

    enabled = [publication for publication in publications if publication.is_enabled]
    if maximum_gridpoint_distance is None:
        maximum_gridpoint_distance = rdfconfig.DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE

    built = {}

    jsonformat_document = jsonformat.build(enabled)
    if jsonformat_document["parameters"]:
        built[JSONFORMAT_PATH] = jsonformat_document

    rawdataforecaster_document = rdfconfig.build(
        enabled,
        bucket=bucket,
        endpoint=endpoint,
        prefix=f"{SINK_ROOT}/",
        region=region,
        maximum_gridpoint_distance=maximum_gridpoint_distance,
    )
    if rawdataforecaster_document["areas"]:
        built[RAWDATAFORECASTER_PATH] = rawdataforecaster_document

    return built


def refresh(publications=None) -> list[str]:
    """Write whichever config documents have changed. Returns the paths written.

    One query, both documents, and a write only where the sha differs. Not an
    optimisation: this runs every five minutes and at the end of every publish,
    and an unconditional write would put a new ``LastModified`` on a file the
    sidecar is polling — which the sidecar cannot distinguish from a real change,
    so every reconciler tick would become a config reload on both processes.
    """
    from .models import FortiPublication, instance_sink

    if publications is None:
        publications = list(
            FortiPublication.objects.filter(is_enabled=True).select_related(
                "collection__catalog__organisation",
            )
        )

    sink = instance_sink()
    written = []

    for path, document in documents(publications, **storage_settings()).items():
        content = encode(document)
        if _current_sha(sink, path) == sha256(content):
            continue
        sink.write(path, content)
        written.append(path)

    return written


def _current_sha(sink, path: str) -> str | None:
    """The sha of what is on the bucket, or None if there is nothing to compare.

    A read that fails is deliberately not distinguished from an absent file: the
    only use of the answer is "may I skip the write", and the safe answer to a
    question that could not be asked is no.
    """
    try:
        if not sink.exists(path):
            return None
        return sha256(sink.read_bytes(path))
    except Exception:
        logger.warning("could not read %s to compare — rewriting", path, exc_info=True)
        return None
