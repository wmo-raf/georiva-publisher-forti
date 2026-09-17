"""The engine: plan, read, derive, stage, and only then publish the markers.

The order is the whole design. Everything up to ``publish_markers`` is invisible
to a reader — objects appearing under a version nothing points at — and the two
markers at the end are what make the version real. M0 confirmed both halves
against met.no's ``rawdataforecaster``: bytes left staged for four seconds with
the marker withheld drew no load attempt at all, and the load began within two
seconds of the marker appearing.

Retention runs *after* the markers, never before. A version is only superseded
once ``latest/<area>`` says so. The serving config runs after both, and after
``mark_ready`` — it advertises what the markers made real, and it reads the field
only ``mark_ready`` writes.
"""

import logging

import numpy as np

from . import derivations
from .grid import from_raster_window
from .planner import NothingToPublish, PublicationRefused, latest_item_geometry, plan
from .reader import WindowReader
from .symbol import weather_symbol
from .windows import find_windows
from .writer import Series, completion_markers, stage_version

logger = logging.getLogger(__name__)

#: How many versions of an area survive a retention pass, counting the one
#: ``latest/<area>`` names. At roughly 3 MB a version, history is nearly free —
#: and being able to point a reader back one version is what makes a bad publish
#: recoverable in seconds.
VERSIONS_KEPT = 5


class GridMoved(Exception):
    """The point list is not the one pinned on the publication.

    Refused rather than published: ``rawdataforecaster`` caches its s2 index
    under the MD5 of the coordinates and never frees it (`dataset.go:123`), so a
    grid that changes shape leaks an index per run — and every stored value is
    addressed by an ordinal into a list that just moved.
    """


def publish(publication, facts: dict | None = None) -> dict:
    """Publish one publication's latest closed run. Returns what it did.

    ``facts`` is the ``build_attempt`` dict, filled in as we go so an attempt that
    dies half-way still reports what it had established.
    """
    facts = facts if facts is not None else {}

    publish_plan = plan(publication)
    facts["version"] = publish_plan.version
    facts["step_count"] = publish_plan.step_count
    facts["parameter_count"] = len(publish_plan.parameters)

    if publication.is_up_to_date(publish_plan.fingerprint):
        logger.info(
            "%s: run %s already published at version %d — nothing to do",
            publication.area_key,
            publish_plan.reference_time,
            publish_plan.version,
        )
        return {"skipped": True, "version": publish_plan.version}

    grid, cubes = _read(publication, publish_plan)
    facts["point_count"] = grid.point_count

    all_series = _derive(publish_plan, grid, cubes)

    sink = publication.sink()
    staged = stage_version(sink, publication.area_key, publish_plan.version, grid, all_series)
    facts["objects_written"] = len(staged["objects"])

    # The markers, last, in order, and only once the bytes they promise exist.
    sink.publish_markers(
        completion_markers(publication.area_key, publish_plan.version, publish_plan.time_until_next),
        require_staged=[staged["witness"]],
    )

    pruned = prune(publication, keep=VERSIONS_KEPT)
    facts["versions_pruned"] = pruned

    publication.mark_ready(
        input_fingerprint=publish_plan.fingerprint,
        grid_id=grid.identifier,
        point_count=grid.point_count,
        # Written back because the planner may have *reset* it: a new run
        # publishes at generation 0 whatever the field said, and a row left
        # reading 4 while its stamp ends 00 is a row whose next hand-raise to 5
        # names bytes that were never written. The plan is the authority here —
        # it is the value the stamp on the bucket was built from.
        generation=publish_plan.generation,
        published_version=publish_plan.version,
        published_reference_time=publish_plan.reference_time,
        published_step_count=publish_plan.step_count,
        published_parameters=[parameter.name for parameter in publish_plan.parameters],
    )

    # The config, last, and inline. It advertises this area and its parameters,
    # so it must follow the markers — but "after the markers" is not enough: it
    # has to follow ``mark_ready``, because ``rdfconfig.servable`` reads
    # ``published_version`` and only ``mark_ready`` sets it. A refresh landing
    # between the two writes an area list that omits the very run that triggered
    # it, and nothing anywhere reports that.
    #
    # Inline rather than queued for exactly that reason: here the ordering is a
    # property of this function, and a queued refresh would make it a property of
    # when a worker happened to pick the message up.
    _refresh_config(publication)

    logger.info(
        "%s: published version %d — %d point(s), %d step(s), %d parameter(s), %d old version(s) pruned",
        publication.area_key,
        publish_plan.version,
        grid.point_count,
        publish_plan.step_count,
        len(publish_plan.parameters),
        pruned,
    )

    return {
        "skipped": False,
        "version": publish_plan.version,
        "points": grid.point_count,
        "steps": publish_plan.step_count,
        "pruned": pruned,
    }


def _refresh_config(publication) -> None:
    """Reconcile the serving configs, without letting them fail a good publish.

    The bytes are on the bucket and ``latest/<area key>`` points at them: the
    publish succeeded whatever happens here, and marking it FAILED would send the
    sweep to redo work that is already done. A config that did not get written is
    a *serving* fault, and the five-minute reconciler is what it is for — so this
    is logged loudly and swallowed.

    It re-reads the publications rather than using the one in hand. The build
    discipline writes every transition with a queryset ``update()``, so this
    instance's ``published_version`` is stale by exactly the ``mark_ready`` that
    just ran — the transition the config is here to describe.
    """
    from . import config

    try:
        written = config.refresh()
    except Exception:
        logger.exception(
            "%s: published, but the serving config was not refreshed — the reconciler will retry",
            publication.area_key,
        )
    else:
        if written:
            logger.info("%s: refreshed %s", publication.area_key, ", ".join(written))


def _read(publication, publish_plan):
    """The grid, and one ``(time, point)`` cube per slot.

    Keyed by **slot**, not by variable slug, which is what lets everything below
    go on saying ``cubes["2t"]`` while the variable behind that slot is whatever
    the publication's mapping named.
    """
    item = latest_item_geometry(publication.collection, publish_plan.reference_time)
    if item is None:
        raise NothingToPublish(f"{publication.collection.slug} has no item for {publish_plan.reference_time}")

    reader = WindowReader(publication.bbox)
    first_href = next(iter(publish_plan.hrefs.values()))[0]
    row_off, col_off, rows, cols = reader.resolve(first_href)
    grid = from_raster_window(reader.transform, rows, cols, row_off, col_off)

    if publication.grid_id and publication.grid_id != grid.identifier:
        raise GridMoved(
            f"{publication.area_key}: the point list changed — pinned {publication.grid_id}, "
            f"now {grid.identifier} ({grid.point_count} points). Either the bbox was edited "
            f"or the source raster's geometry moved; both invalidate every stored ordinal."
        )

    cubes = {slot: reader.read_series(hrefs) for slot, hrefs in publish_plan.hrefs.items()}
    return grid, cubes


def _derive(publish_plan, grid, cubes) -> list[Series]:
    """Every published series, in ``meta.json`` order.

    Windows are found once per length and shared by every parameter of that
    length, which is also what keeps a symbol and its precipitation describing
    the same span.
    """
    windows_by_hours = {}
    for parameter in publish_plan.parameters:
        if parameter.is_period and parameter.offset not in windows_by_hours:
            windows_by_hours[parameter.offset] = find_windows(publish_plan.times, parameter.offset)

    all_series = []
    for parameter in publish_plan.parameters:
        windows = windows_by_hours.get(parameter.offset, [])
        times = [window.end_time for window in windows] if parameter.is_period else publish_plan.times
        values = _values_for(parameter, publish_plan, grid, cubes, windows)
        all_series.append(Series(parameter=parameter, times=times, values=values))

    return all_series


def _values_for(parameter, publish_plan, grid, cubes, windows) -> np.ndarray:
    if parameter.source:
        return cubes[parameter.source]

    if parameter.derivation == "relative_humidity":
        return derivations.relative_humidity(cubes["2t"], cubes["2d"])

    if parameter.derivation == "precipitation_window":
        return derivations.accumulation_window(cubes["tp"], windows)

    if parameter.derivation == "temperature_max":
        return derivations.window_max(cubes["2t"], windows)

    if parameter.derivation == "temperature_min":
        return derivations.window_min(cubes["2t"], windows)

    if parameter.derivation == "weather_symbol":
        return weather_symbol(
            precipitation_mm=derivations.accumulation_window(cubes["tp"], windows),
            cloud_percent=derivations.window_mean(cubes["tcc"], windows),
            end_times=[window.end_time for window in windows],
            latitude=grid.latitude,
            longitude=grid.longitude,
            hours=parameter.offset,
        ).astype(np.float32)

    raise PublicationRefused(f"No derivation named {parameter.derivation!r} for {parameter.name}")


def prune(publication, keep: int = VERSIONS_KEPT) -> int:
    """Drop all but the newest ``keep`` versions of this area.

    Count-based rather than age-based, and the version ``latest/<area key>``
    names is never a candidate however old it is — an area whose feed has
    stopped still has a reader following its last good version.

    A superseded version is deleted **whole**, its own ``complete.json``
    included: that file is a completion marker, and a version directory that
    cannot lose it never goes away.

    Every path below is derived from :attr:`area_key`, and that is the whole of
    retention's tenancy safety now that the root is shared. Core refuses only
    ``delete_prefix("")``; it cannot refuse ``delete_prefix("latest")`` or
    ``("config")``, because which names under the root are areas is this
    plugin's grammar and not core's. What makes those unreachable from here is
    that an area key always contains a ``.`` and those names never do — so no
    publication can name one, whatever it is called.
    """
    sink = publication.sink()
    area_key = publication.area_key

    try:
        current = int(sink.read_bytes(publication.marker_path()).decode().strip())
    except Exception:
        logger.warning(
            "%s: cannot read %s — skipping retention rather than guessing which version a reader is following",
            area_key,
            publication.marker_path(),
        )
        return 0

    versions = sorted(
        (int(name) for name in sink.children(area_key) if name.isdigit()),
        reverse=True,
    )
    doomed = [version for version in versions[keep:] if version != current]

    for version in doomed:
        sink.delete_prefix(publication.version_prefix(version), include_markers=True)
        logger.info("%s: pruned version %d", area_key, version)

    return len(doomed)
