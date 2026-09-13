"""What this instance intends to serve, and what is actually serving it.

Four hops, compared by sha (D23):

    intended  →  bucket  →  volume  →  loaded

The database decides; :mod:`config` writes to ``_forti/config/``; the ``mc``
sidecar copies that into the pair's config volume; ``configwatch`` inside each
process reads it and says what it loaded. Every hop is a different machine's
opinion of the same bytes, and each one can be stuck without the next one
knowing — which is the reason to read all four rather than any one of them.

**Read-only, and that is the decision rather than the limitation** (D23).
GeoRiva's database is the single authority and ``refresh_forti_config`` rewrites
the bucket from it every five minutes, so a form that wrote a config document
would be reverted within one reconciler tick while still showing what somebody
typed. The honest surface for a fact you may not change is one that does not
offer to.

Four things the plan left open are settled here, and each is written where it is
decided as well as in the commit that made it:

**Who may see it: the instance admin, and nobody else.** See
:class:`~.wagtail_hooks.SuperuserMenuItem` and ``verification_panel``. Every
figure below is instance-wide — ``rawdataforecaster.json`` names *every*
organisation's area keys, and one sha describes one document governing every
tenant — so there is nothing here that narrows to an organisation, and the
obvious default (render what you read, inside one organisation's admin) would
hand ``ke-kmd.ecmwf-ifs`` to another organisation's administrator. The deciding
argument is not the leak but the audience: every action this page prompts —
restart the pair, fix the endpoint, re-fetch the compose file — belongs to the
person who deployed it. What that gives up is that an organisation administrator
cannot see whether their own model is resident; that question is about one
publication and belongs beside it, not here.

**When the reads happen: on request, under one deadline, never cached.** An
operator loads this to answer "is it working *now*", and a cache would answer a
question about the recent past at exactly the moment the difference matters. The
risk a cache would have bought down — an admin worker held open while MinIO does
not answer — is bought down instead by the thing that actually causes it: the
whole set of reads runs in one worker thread with one deadline, so the page
renders either the readings or "the bucket did not answer", and never blocks
past it. See :func:`_readings`.

**What a failed read renders as: not the same thing as no file yet.** Both are
true of every hop on this instance today — nothing is published under ``_forti/``
and the pair has never been started here — so a panel that conflated them would
be developed in the one state where the conflation is invisible, and would then
report an outage as "not cut over yet" forever after. The six presences below
are the whole of that distinction.

**What agreement looks like:** each hop carries its own presence and its own
sha, the first twelve hex characters shown with the rest on hover, and agreement
is stated against hop 1 rather than against the hop before it — so a chain
reports *where* it broke and not merely that it did. A hop that has not happened
is not a mismatch and does not render as one.

**One trap with no home anywhere else.** ``loaded_sha`` is the digest of the
bytes the process last *attempted*, not of the configuration it is serving:
``apply`` (`configwatch.go:256`) records the digest whatever the outcome, and a
rejected document leaves the previous configuration running. A chain whose
fourth hop carries the intended sha with ``ok: false`` therefore means the
document arrived and was **refused** — four matching shas and a process serving
something else. :data:`REFUSED` exists so that cannot render as agreement.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import PurePosixPath

from django.conf import settings
from django.utils.dateparse import parse_datetime

from . import config, rdfconfig
from .config import ABSENT, PRESENT, UNREACHABLE

logger = logging.getLogger(__name__)

#: Hop 1 only. No enabled publication would produce a document this process
#: would accept, so :mod:`config` writes nothing — which means "leave what is on
#: the bucket alone" and never "delete it". It is not a mismatch and must not
#: render as one.
WITHHELD = "withheld"

#: Something is at this path and it is not the document it claims to be.
#: ``status/rawdataforecaster.json`` and ``config/rawdataforecaster.json`` share
#: a basename and mean opposite things; the sidecar keeps them apart with two
#: guards — a config document containing ``loaded_sha`` is refused on the way
#: down, a status document with no ``module`` is refused on the way up — and this
#: reader is the third place that could confuse them. It checks ``module``
#: against the name it asked for, which is the same guard one step stricter.
FOREIGN = "foreign"

#: Hop 4 only. The process read this document and rejected it; the configuration
#: it is serving is the previous one. See the module docstring.
REFUSED = "refused"

#: Where each process leaves its status. ``configwatch`` writes
#: ``<status-dir>/<module>.json`` (`configwatch.go:217`) and the sidecar copies
#: the directory up under its own basenames, so the module name is both the file
#: name and the field this reader checks it against.
STATUS_PREFIX = "status"

SIDECAR_MODULE = "sidecar"
RAWDATAFORECASTER_MODULE = "rawdataforecaster"
JSONFRONTEND_MODULE = "jsonfrontend"

SIDECAR_PATH = f"{STATUS_PREFIX}/{SIDECAR_MODULE}.json"
RAWDATAFORECASTER_STATUS_PATH = f"{STATUS_PREFIX}/{RAWDATAFORECASTER_MODULE}.json"
JSONFRONTEND_STATUS_PATH = f"{STATUS_PREFIX}/{JSONFRONTEND_MODULE}.json"


@dataclass(frozen=True)
class ChainSpec:
    """One process, the config document it reads, and the status file it writes.

    Named fields rather than a bare triple, and the reason is the same trap the
    sidecar spends four guards on: ``config/rawdataforecaster.json`` and
    ``status/rawdataforecaster.json`` share a basename and mean opposite things.
    Unpacked positionally this was read three different ways at six call sites —
    ``for _, path, _``, ``for module, _, path``, ``for module, path, _`` — and a
    single transposition would have had the panel comparing a status document's
    sha against the intended config, silently, in the one module whose whole job
    is to notice that.
    """

    module: str
    config_path: str
    status_path: str


#: Which config document each process reads, and therefore which chain its
#: status file terminates. The sidecar reports the volume hop for both under the
#: documents' **basenames**, because the volume is a directory and has no prefix.
CHAINS = (
    ChainSpec(RAWDATAFORECASTER_MODULE, config.RAWDATAFORECASTER_PATH, RAWDATAFORECASTER_STATUS_PATH),
    ChainSpec(JSONFRONTEND_MODULE, config.JSONFORMAT_PATH, JSONFRONTEND_STATUS_PATH),
)

#: Seconds the whole set of remote reads is allowed. Deliberately short: this is
#: an admin page, the reads are a handful of HEADs and GETs against object
#: storage on the same network, and the alternative to giving up is holding a
#: worker for botocore's default retry ladder — which is minutes, five times
#: over. A deployment whose storage is genuinely slow can raise it.
DEFAULT_DEADLINE = 5.0

#: The distribution whose version the sidecar's ``FORTI_COMPOSE_VERSION`` is
#: compared against. ``test_compose.py`` makes the same comparison at build time
#: so the two constants cannot already be equal by construction; this makes it
#: at run time, because an operator fetches the compose file by hand and
#: installs the plugin through ``plugins.toml``, and the two drift.
DISTRIBUTION = "georiva-publisher-forti"


# =============================================================================
# What one reading is
# =============================================================================


#: What each badge says out loud. Deliberately not the presence constant: an
#: operator reads "not yet" and "could not read" as different afternoons, which
#: is the whole point of their being different states.
_BADGE_LABELS = {
    "intended": "the reference",
    "agrees": "agrees",
    "differs": "differs",
    ABSENT: "not yet",
    UNREACHABLE: "could not read",
    WITHHELD: "nothing to write",
    FOREIGN: "not this document",
    REFUSED: "refused",
}


@dataclass(frozen=True)
class Hop:
    """One machine's answer about one document."""

    name: str
    label: str
    where: str
    presence: str
    sha: str | None = None
    detail: str = ""
    #: Against **hop 1**, not against the hop before. ``None`` where the
    #: comparison cannot be made — which is every presence but
    #: :data:`~.config.PRESENT` and :data:`REFUSED`, and is not the same answer
    #: as ``False``.
    agrees: bool | None = None

    @property
    def short(self) -> str:
        """Twelve hex characters. Enough that two documents on one instance do
        not collide, short enough to compare four of them by eye; the template
        carries the whole digest in a ``title``."""
        return self.sha[:12] if self.sha else ""

    @property
    def badge(self) -> str:
        """The one word this hop gets, decided here rather than in the template.

        Six presences and a three-valued ``agrees`` make eight outcomes, and a
        template that worked them out with ``{% if %}`` would be the second
        place the vocabulary is written down — the place nobody tests. The CSS
        class is this string, so a state with no rule renders unstyled rather
        than as somebody else's colour.
        """
        if self.presence != PRESENT:
            return self.presence
        if self.agrees is None:
            return "intended"
        return "agrees" if self.agrees else "differs"

    @property
    def badge_label(self) -> str:
        return _BADGE_LABELS.get(self.badge, self.badge)


@dataclass(frozen=True)
class Chain:
    """One document's four hops, and what they add up to."""

    document: str
    reader: str
    path: str
    hops: tuple[Hop, ...]
    verdict: str
    #: The name of the first hop that does not carry what hop 1 intends, or
    #: ``None`` when every hop does. The answer to "where did it stop".
    broken_at: str | None = None


@dataclass(frozen=True)
class AreaRow:
    """One configured area: what GeoRiva published, and what is resident.

    ``available`` and ``loaded`` are ``*int`` with no ``omitempty`` in the fork
    (`forecast.go:392`), so both keys are always present and ``null`` is an
    answer rather than an absence. Telling ``null`` from ``0`` is the whole of
    M5.1's first fix — a Go map miss reading as version 0 is what made one
    unpublished area fatal for every organisation — and this row is where an
    operator finally sees the difference.
    """

    area: str
    published: int | None
    available: int | None
    loaded: int | None
    verdict: str
    ok: bool


@dataclass(frozen=True)
class Module:
    """One process's ``configwatch.Status``."""

    module: str
    presence: str
    loaded_sha: str | None = None
    loaded_at: str = ""
    age: str = ""
    ok: bool | None = None
    errors: tuple[str, ...] = ()
    applied: tuple[str, ...] = ()
    pending_restart: tuple[str, ...] = ()
    detail: str = ""

    @property
    def reported(self) -> bool:
        return self.presence == PRESENT


@dataclass(frozen=True)
class Sidecar:
    """``sidecar.json`` — the only reporter of the third hop."""

    presence: str
    compose_version: str = ""
    checked_at: str = ""
    age: str = ""
    interval_seconds: int | None = None
    fetch_ok: bool | None = None
    upload_ok: bool | None = None
    volume: dict = field(default_factory=dict)
    detail: str = ""

    @property
    def reported(self) -> bool:
        return self.presence == PRESENT

    @property
    def lag(self) -> str:
        """How much staleness is ordinary at the hops below this one.

        Here rather than beside the verdict that prints it: it reads nothing but
        this document, and a verdict about a *volume* that quoted the sidecar's
        interval from somewhere else would be the second place that fact lives.
        """
        if self.reported and self.interval_seconds:
            return (
                f"The sidecar passes every {self.interval_seconds} s and last ran "
                f"{self.age or 'at an unknown time'}, so one pass of lag is ordinary here and longer is not."
            )
        return "One sidecar pass of lag is ordinary here; longer is not."

    @property
    def troubled(self) -> bool:
        """A pass that ran and did not do its job.

        Worth its own flag rather than left as two fields in a list: a sidecar
        that has been failing to fetch for two days is the *cause* of a volume
        hop that disagrees, and an operator reading the chain above should be
        pointed at it rather than left to notice a "no" among four figures.
        """
        return self.reported and (self.fetch_ok is False or self.upload_ok is False)


@dataclass(frozen=True)
class Versions:
    """The compose file's idea of itself against the installed plugin's."""

    plugin: str
    compose: str = ""
    agree: bool | None = None
    detail: str = ""


@dataclass(frozen=True)
class Report:
    read_at: datetime
    deadline: float
    answered: bool
    chains: tuple[Chain, ...]
    sidecar: Sidecar
    modules: tuple[Module, ...]
    areas: tuple[AreaRow, ...]
    areas_detail: str
    store_error: str
    versions: Versions


@dataclass(frozen=True)
class _Status:
    """A status document as far as we could get, before it is interpreted."""

    presence: str
    payload: dict = field(default_factory=dict)
    detail: str = ""


# =============================================================================
# Reading
# =============================================================================


def read_status(sink, path: str, module: str) -> _Status:
    """One status document, refused unless it says it is the one asked for.

    The third guard against the basename collision. ``status/…`` and
    ``config/…`` share a name for ``rawdataforecaster``, and the two directions
    mean opposite things; the sidecar refuses a config document containing
    ``loaded_sha`` on the way down and a status document with no ``module`` on
    the way up. This is stricter than either: the ``module`` field has to be the
    one whose status file this is, so a status document that arrived under the
    wrong basename is reported rather than read.

    The config side gets no mirror of this because it needs none and would gain
    a worse failure mode: a config path holding the wrong document shows up as
    hop 2 disagreeing with hop 1, which is exactly what it is, and
    ``refresh_forti_config`` rewrites the right document over it within five
    minutes. Seeing the repair happen is better than being told about it.
    """
    try:
        if not sink.exists(path):
            return _Status(ABSENT)
        payload = sink.read_json(path)
    except Exception as exc:
        logger.warning("could not read %s from the bucket", path, exc_info=True)
        return _Status(UNREACHABLE, detail=f"{type(exc).__name__}: {exc}")

    if not isinstance(payload, dict):
        return _Status(FOREIGN, detail=f"{path} is not a JSON object.")

    found = payload.get("module")
    if found != module:
        return _Status(
            FOREIGN,
            detail=(
                f"{path} says it is {found!r}, not {module!r}. A status document under the "
                f"wrong basename is refused rather than read: config/ and status/ share names."
            ),
        )
    return _Status(PRESENT, payload=payload)


def _read_all(sink) -> dict:
    """Every remote read this page makes. One function so one deadline covers
    the set rather than each read separately — five reads with their own
    deadlines is five deadlines long."""
    return {
        "config": {spec.config_path: config.current(sink, spec.config_path) for spec in CHAINS},
        "status": {
            SIDECAR_PATH: read_status(sink, SIDECAR_PATH, SIDECAR_MODULE),
            **{spec.status_path: read_status(sink, spec.status_path, spec.module) for spec in CHAINS},
        },
    }


def _readings(sink, deadline: float):
    """The readings, or ``None`` if the bucket did not answer in time.

    The thread is not cancellable — a socket blocked in ``recv`` does not care
    that nobody is waiting — so ``shutdown(wait=False)`` is the point of this
    function rather than an oversight: the orphan finishes or times out on
    botocore's own schedule, in the background, while the request returns. The
    alternative is the admin page holding a worker through the full retry
    ladder, which is the failure this deadline exists for.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="forti-verification")
    try:
        return pool.submit(_read_all, sink).result(timeout=deadline)
    except FutureTimeout:
        logger.warning("the publications bucket did not answer within %ss", deadline)
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# =============================================================================
# Interpreting
# =============================================================================


def _age(raw) -> str:
    """How long ago, from the timestamp **inside** the document.

    Never from the object's ``LastModified``. The sidecar's ``push_status``
    uploads every pass whether or not anything moved — deliberately, and it says
    so — so an object's modification time reports the sidecar's liveness and
    tells you nothing about the process whose document it is.
    """
    if not raw:
        return ""
    moment = parse_datetime(raw) if isinstance(raw, str) else None
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    seconds = int((datetime.now(UTC) - moment).total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _strings(payload, key) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _sidecar(reading: _Status) -> Sidecar:
    if reading.presence != PRESENT:
        return Sidecar(
            presence=reading.presence,
            detail=reading.detail
            or ("Nothing has written _forti/status/sidecar.json. The pair has never been started against this bucket."),
        )

    payload = reading.payload
    volume = payload.get("volume")
    checked_at = str(payload.get("checked_at") or "")
    return Sidecar(
        presence=PRESENT,
        compose_version=str(payload.get("compose_version") or ""),
        checked_at=checked_at,
        age=_age(checked_at),
        interval_seconds=payload.get("interval_seconds"),
        fetch_ok=payload.get("fetch_ok"),
        upload_ok=payload.get("upload_ok"),
        volume=volume if isinstance(volume, dict) else {},
    )


def _module(module: str, reading: _Status) -> Module:
    if reading.presence != PRESENT:
        return Module(
            module=module,
            presence=reading.presence,
            detail=reading.detail or f"{module} has never written a status file to this bucket.",
        )

    payload = reading.payload
    loaded_at = str(payload.get("loaded_at") or "")
    return Module(
        module=module,
        presence=PRESENT,
        loaded_sha=str(payload.get("loaded_sha") or "") or None,
        loaded_at=loaded_at,
        age=_age(loaded_at),
        ok=payload.get("ok"),
        errors=_strings(payload, "errors"),
        applied=_strings(payload, "applied"),
        pending_restart=_strings(payload, "pending_restart"),
    )


def _versions(sidecar: Sidecar) -> Versions:
    try:
        installed = version(DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover - the plugin is always installed to run
        installed = ""

    if sidecar.presence != PRESENT or not sidecar.compose_version:
        return Versions(
            plugin=installed,
            agree=None,
            detail="The sidecar has not reported a compose version, so there is nothing to compare.",
        )

    if sidecar.compose_version == installed:
        return Versions(plugin=installed, compose=sidecar.compose_version, agree=True)

    return Versions(
        plugin=installed,
        compose=sidecar.compose_version,
        agree=False,
        detail=(
            f"The running compose file is {sidecar.compose_version}; the installed plugin is "
            f"{installed}. Operators fetch the compose file by hand and install the plugin "
            f"through plugins.toml, so the pairing is manual and this is how it drifts. Fetch "
            f"deploy/compose.yml at the tag pinned for this plugin."
        ),
    )


# =============================================================================
# The chain
# =============================================================================


def _intended_hop(path: str, document: str, intended: dict) -> Hop:
    sha = intended.get(path)
    if sha is not None:
        return Hop("intended", "Intended", "GeoRiva's database", PRESENT, sha=sha)
    return Hop(
        "intended",
        "Intended",
        "GeoRiva's database",
        WITHHELD,
        detail=(
            f"No enabled publication would produce a {document} either process would accept, "
            f"so GeoRiva writes none. Whatever is on the bucket is left exactly as it is — "
            f"an empty document is fatal at the next restart, and a stale one is not."
        ),
    )


def _bucket_hop(path: str, reading: config.Current) -> Hop:
    where = f"_forti/{path}"
    if reading.presence == PRESENT:
        return Hop("bucket", "On the bucket", where, PRESENT, sha=reading.sha)
    if reading.presence == ABSENT:
        return Hop(
            "bucket",
            "On the bucket",
            where,
            ABSENT,
            detail=(
                "Nothing has been written here. Either this instance has not cut over yet, or the refresh has not run."
            ),
        )
    return Hop("bucket", "On the bucket", where, UNREACHABLE, detail=reading.error or "")


def _volume_hop(document: str, sidecar: Sidecar) -> Hop:
    where = f"the pair's config volume, /config/{document}"

    if sidecar.presence != PRESENT:
        presence = ABSENT if sidecar.presence == ABSENT else sidecar.presence
        return Hop("volume", "In the volume", where, presence, detail=sidecar.detail)

    if document not in sidecar.volume:
        return Hop(
            "volume",
            "In the volume",
            where,
            FOREIGN,
            detail=(
                f"sidecar.json reports no entry for {document}. Its volume map always carries "
                f"both documents — a file that is not there prints as null — so this is a "
                f"sidecar written by a different compose file."
            ),
        )

    sha = sidecar.volume[document]
    if sha:
        return Hop("volume", "In the volume", where, PRESENT, sha=str(sha))
    return Hop(
        "volume",
        "In the volume",
        where,
        ABSENT,
        detail="The sidecar has not put this document in the volume. There was nothing on the bucket to fetch.",
    )


def _loaded_hop(module: str, status: Module) -> Hop:
    where = f"{module}, as configwatch reports it"

    if status.presence != PRESENT:
        return Hop("loaded", "Loaded", where, status.presence, detail=status.detail)

    if not status.loaded_sha:
        return Hop(
            "loaded",
            "Loaded",
            where,
            ABSENT,
            detail=(
                "The process has written a status file but names no configuration — it could "
                "not read the file at all. " + "; ".join(status.errors)
            ).strip(),
        )

    if status.ok is False:
        return Hop(
            "loaded",
            "Loaded",
            where,
            REFUSED,
            sha=status.loaded_sha,
            detail=(
                "This document was read and rejected; the process is still serving the "
                "configuration it had. " + "; ".join(status.errors)
            ).strip(),
        )

    return Hop("loaded", "Loaded", where, PRESENT, sha=status.loaded_sha)


def _compared(hops: tuple[Hop, ...]) -> tuple[Hop, ...]:
    """Each hop's agreement with **hop 1**.

    Against the first rather than against its predecessor, so a chain says where
    it broke: compared pairwise, one stale hop makes every hop after it disagree
    with its neighbour and the operator is left counting crosses to find the
    join. ``None`` is a third answer and not a weak ``False`` — a hop with no sha
    has not disagreed with anything.

    A :data:`REFUSED` hop never agrees, whatever its sha says, because the sha
    names what was read and not what is running.
    """
    reference, rest = hops[0], hops[1:]
    intended = reference.sha if reference.presence == PRESENT else None

    compared = [reference]
    for hop in rest:
        if intended is None or hop.sha is None:
            agrees = None
        elif hop.presence == REFUSED:
            agrees = False
        else:
            agrees = hop.sha == intended
        compared.append(
            Hop(hop.name, hop.label, hop.where, hop.presence, sha=hop.sha, detail=hop.detail, agrees=agrees)
        )
    return tuple(compared)


def _verdict(document: str, hops: tuple[Hop, ...], sidecar: Sidecar) -> tuple[str, str | None]:
    """One sentence, and the name of the hop it is about."""
    intended = hops[0]

    if intended.presence == WITHHELD:
        carried = [hop for hop in hops[1:] if hop.sha]
        if carried:
            return (
                f"GeoRiva would write no {document} right now, so nothing is being compared — "
                f"the pair is running on the last document that was written, which is the "
                f"correct outcome and not a stale one.",
                None,
            )
        # "No sha anywhere" is not the same as "nothing was ever written": a hop
        # that could not be read has no sha either, and asserting that none ever
        # reached the pair on the strength of a question that was not answered is
        # the exact collapse the six presences exist to prevent.
        unread = [hop for hop in hops[1:] if hop.presence in (UNREACHABLE, FOREIGN)]
        if unread:
            return (
                f"GeoRiva would write no {document}. {unread[0].label}: could not be read, so "
                f"whether the pair is running on an earlier one is unknown.",
                unread[0].name,
            )
        return (f"GeoRiva would write no {document}, and none has ever reached the pair.", "bucket")

    for hop in hops[1:]:
        if hop.agrees:
            continue
        # Every verdict is "<the hop that stopped it>: <why>", because the hop
        # labels are prepositional — "On the bucket", "In the volume" — and read
        # as nonsense used as the subject of a sentence. The colon also makes the
        # first word of the answer the operator's next question.
        if hop.presence == UNREACHABLE:
            return (f"{hop.label}: could not be read. {hop.detail}", hop.name)
        if hop.presence == PRESENT:
            return (
                f"{hop.label}: a different document from the one GeoRiva intends. {sidecar.lag}",
                hop.name,
            )
        # ABSENT, FOREIGN and REFUSED each carry a whole sentence of their own.
        return (f"{hop.label}: {hop.detail}", hop.name)

    return (f"Every hop carries the {document} GeoRiva intends.", None)


def _chain(spec: ChainSpec, intended: dict, bucket: config.Current, sidecar: Sidecar, status: Module) -> Chain:
    document = PurePosixPath(spec.config_path).name
    hops = _compared(
        (
            _intended_hop(spec.config_path, document, intended),
            _bucket_hop(spec.config_path, bucket),
            _volume_hop(document, sidecar),
            _loaded_hop(spec.module, status),
        )
    )
    verdict, broken_at = _verdict(document, hops, sidecar)
    return Chain(
        document=document, reader=spec.module, path=spec.config_path, hops=hops, verdict=verdict, broken_at=broken_at
    )


# =============================================================================
# The areas
# =============================================================================


def _area_verdict(
    published: int | None, available: int | None, loaded: int | None, store_error: str
) -> tuple[str, bool]:
    """One sentence per row, and it must not send the reader somewhere the page
    has not rendered.

    ``store_error`` is a parameter rather than read from module state for exactly
    that: "no marker" means two different things depending on whether the listing
    that failed to find one had itself failed, and only one of the two is a
    pointer at the banner above.
    """
    if loaded is None and available is None:
        if published is None:
            return ("Nothing is published under this key and nothing is resident.", False)
        if store_error:
            return (
                f"GeoRiva has published version {published} and the reader found no marker for "
                f"it — but its listing is failing, so this figure proves nothing. See above.",
                False,
            )
        return (
            f"GeoRiva has published version {published}; the reader's last successful listing of "
            f"_forti/latest/ found no marker for it. The area is configured and off the air.",
            False,
        )
    if loaded is None:
        return (f"Version {available} is on the bucket and nothing is resident yet.", False)
    if available is None:
        # Resident, and the reader's marker map has no entry. It loaded from a
        # marker that is no longer there, so the process is serving data nothing
        # would load again — and every later branch would have called that
        # "serving the version GeoRiva published", in green, beside a cell
        # reading "no marker".
        if store_error:
            return (
                f"Serving {loaded}, but the reader cannot list the store, so whether a marker "
                f"still names that version is unknown. See above.",
                False,
            )
        return (
            f"Serving {loaded}, but the reader's last successful listing found no "
            f"latest/ marker for this area. Nothing would load it again.",
            False,
        )
    if loaded < available:
        return (f"Serving {loaded}; {available} is on the bucket and not loaded.", False)
    if published is not None and loaded < published:
        return (
            f"Serving {loaded}; GeoRiva has since published {published}, which the reader's last listing had not seen.",
            False,
        )
    if published is None:
        return (f"Serving {loaded}, for an area no publication on this instance names.", False)
    return (f"Serving {loaded}, the version GeoRiva published.", True)


def _areas(state, publications) -> tuple[tuple[AreaRow, ...], str, str]:
    """The rows, why there are none, and the store error.

    Configured areas only and in the order the configuration lists them, which
    is the fork's rule (`forecast.go:414`) and not this reader's to change: an
    area on the bucket that nobody asked for is not that process's business.
    Areas GeoRiva intends that the reader does not list are appended after,
    because their absence is a fact about the configuration hop and would
    otherwise show only as a sha that disagrees.
    """
    published = {
        publication.area_key: publication.published_version for publication in rdfconfig.servable(publications)
    }

    if not isinstance(state, dict):
        detail = (
            "This rawdataforecaster reports no state. M5.1's second fix — the status file "
            "listing resident areas — is not in the image that is running, so a config that "
            "parsed is all this instance can prove."
        )
        return ((), detail, "")

    store_error = str(state.get("store_error") or "")
    reported = state.get("areas")
    rows = []
    seen = set()

    for entry in reported if isinstance(reported, list) else []:
        if not isinstance(entry, dict):
            continue
        area = str(entry.get("area") or "")
        seen.add(area)
        loaded = entry.get("loaded")
        available = entry.get("available")
        verdict, ok = _area_verdict(published.get(area), available, loaded, store_error)
        rows.append(
            AreaRow(
                area=area,
                published=published.get(area),
                available=available,
                loaded=loaded,
                verdict=verdict,
                ok=ok,
            )
        )

    for area in sorted(set(published) - seen):
        rows.append(
            AreaRow(
                area=area,
                published=published[area],
                available=None,
                loaded=None,
                verdict="Not in the configuration this reader is running — it is behind the area list above.",
                ok=False,
            )
        )

    detail = "" if rows else "The reader is configured with no areas."
    return (tuple(rows), detail, store_error)


# =============================================================================
# The report
# =============================================================================


def deadline_seconds() -> float:
    return float(getattr(settings, "GEORIVA_FORTI_VERIFICATION_DEADLINE", DEFAULT_DEADLINE))


def report(*, sink=None, deadline: float | None = None) -> Report:
    """Everything the panel renders, in one call and with no request in it.

    A plain function over a sink so the whole of it is testable without an HTTP
    layer: the states worth asserting — an outage that is not an empty bucket, a
    document refused rather than loaded, a hop that has not happened rather than
    one that disagrees — are all states of the *bucket*, and building them
    through a view would mean building a view to observe them.
    """
    from .models import instance_sink

    if sink is None:
        sink = instance_sink()
    if deadline is None:
        deadline = deadline_seconds()

    publications = config.publications_to_serve()
    intended = config.intended(publications)

    readings = _readings(sink, deadline)
    answered = readings is not None
    if readings is None:
        timed_out = f"The bucket did not answer within {deadline:g} s."
        readings = {
            "config": {spec.config_path: config.Current(UNREACHABLE, error=timed_out) for spec in CHAINS},
            "status": dict.fromkeys(
                (SIDECAR_PATH, RAWDATAFORECASTER_STATUS_PATH, JSONFRONTEND_STATUS_PATH),
                _Status(UNREACHABLE, detail=timed_out),
            ),
        }

    sidecar = _sidecar(readings["status"][SIDECAR_PATH])
    modules = {spec.module: _module(spec.module, readings["status"][spec.status_path]) for spec in CHAINS}

    chains = tuple(
        _chain(spec, intended, readings["config"][spec.config_path], sidecar, modules[spec.module]) for spec in CHAINS
    )

    forecaster = readings["status"][RAWDATAFORECASTER_STATUS_PATH]
    areas, areas_detail, store_error = _areas(
        forecaster.payload.get("state") if forecaster.presence == PRESENT else None,
        publications,
    )
    if forecaster.presence != PRESENT:
        areas_detail = modules[RAWDATAFORECASTER_MODULE].detail

    return Report(
        read_at=datetime.now(UTC),
        deadline=deadline,
        answered=answered,
        chains=chains,
        sidecar=sidecar,
        modules=tuple(modules[spec.module] for spec in CHAINS),
        areas=areas,
        areas_detail=areas_detail,
        store_error=store_error,
        versions=_versions(sidecar),
    )
