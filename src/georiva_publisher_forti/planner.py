"""Deciding what one publish would write, before writing any of it.

The plan is built entirely from the database — no raster is opened — so an
unpublishable run is refused in milliseconds rather than after 70 MB of reads.
Four things it settles:

**Which run.** The latest *closed* ``RunIngestion``. Closed is an optimisation
rather than a guarantee — a run reopens on any later arrival and closes again at a
higher revision — which is exactly why the version carries the revision: a
republish always outranks its predecessor, and a backfilled older run never
outranks a newer one.

**Which version.** ``run.version * 100 + generation``, ordered model time, then
republish of that run, then configuration generation (D25, superseding D8). The
run's own version answers *"which run is this?"*; a reader that reloads only on a
strictly greater integer needs the answer to *"are these the same bytes I already
have?"*, and the two part company the moment anything but the run can change the
bytes. The generation is the term that moves when the run does not, and it is
reset here rather than stored as a second authority — see :func:`_generation_for`.

**Which steps.** The **intersection** across every variable the publication reads.
Variables of one run ingest independently and really do finish at different step
counts — 13 to 16 of 16 observed on a live run — and a step present for some
variables and missing for others is a partial forecast, which Forti has no way to
express. Publishing the intersection is the only honest answer.

**Which parameters.** Those whose windows the run's own time index can express.
ECMWF is 3-hourly to 144 h and 6-hourly beyond, so a 6-hour window exists
throughout and a 1-hour window exists nowhere; the 1-hour parameters drop out
without anybody configuring it.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from georiva.core.models import Asset, Item
from georiva.ingestion.models import RunIngestion

from . import parameters as params
from .models import GENERATION_CEILING
from .windows import publishable


class NothingToPublish(Exception):
    """The run cannot be published, and no amount of retrying changes that.

    Distinct from a failure: the publication is parked as NO_DATA rather than
    FAILED, because the sweep retrying it every five minutes cannot help.
    """


class PublicationRefused(Exception):
    """The publication is configured in a way that would publish wrong data."""


@dataclass
class PublishPlan:
    """Everything the writer needs, and nothing it has to look up again."""

    publication: object
    run: object
    reference_time: datetime
    run_version: int
    generation: int
    times: list
    parameters: list
    hrefs: dict = field(default_factory=dict)
    """``{variable_slug: [href per step, in ``times`` order]}``."""

    time_until_next: timedelta | None = None

    @property
    def version(self) -> int:
        """The integer ``latest/<area key>`` will hold.

        Derived rather than stored so that the two halves cannot be set
        inconsistently: everything downstream — the key prefix, the pointer, the
        stored ``published_version`` — reads this one expression.
        """
        return self.run_version * GENERATION_CEILING + self.generation

    @property
    def step_count(self) -> int:
        return len(self.times)

    @property
    def fingerprint(self) -> str:
        """Identity of the inputs, for the build's skip check.

        The run version alone is not enough in either direction. A run whose
        *step count* grew while its version stayed put is a different publish — a
        variable finishing late extends the intersection — so the fingerprint
        covers what was actually read. And a *configuration* change moves none of
        the run, the step count, the valid times or the parameter names, so
        without the generation the skip check returns early on inputs that are
        genuinely unchanged and the bump that was the whole point is never
        written. That is the trap the slug rename hit in the cutover.
        """
        material = "|".join(
            [
                str(self.run_version),
                str(self.generation),
                str(self.step_count),
                self.times[0].isoformat() if self.times else "",
                self.times[-1].isoformat() if self.times else "",
                ",".join(parameter.name for parameter in self.parameters),
            ]
        )
        return hashlib.sha256(material.encode()).hexdigest()[:32]


def plan(publication) -> PublishPlan:
    """What publishing this publication right now would write.

    Raises ``NothingToPublish`` when there is no closed run or no step every
    variable shares, and ``PublicationRefused`` when a variable is missing or
    carries the wrong unit.
    """
    collection = publication.collection
    _refuse_unpublishable_collection(collection)

    run = RunIngestion.latest_closed(collection)
    if run is None:
        raise NothingToPublish(
            f"{collection.slug} has no closed run. A run closes when its arrival route "
            f"says the last file landed; until then there is no complete forecast to "
            f"transpose."
        )

    needed = params.required_variables(params.ALL_PARAMETERS)
    variables = _resolve_variables(collection, needed)

    hrefs_by_variable = _cog_hrefs(collection, run.reference_time, variables)
    times = _shared_times(hrefs_by_variable)
    if not times:
        raise NothingToPublish(
            f"{collection.slug} @ {run.reference_time:%Y-%m-%dT%H:%MZ}: no timestep has a COG "
            f"for every variable. Variables of one run ingest independently, so this "
            f"resolves itself as the stragglers land."
        )

    chosen = publishable(times, params.ALL_PARAMETERS)
    if not any(parameter.is_period for parameter in chosen):
        raise NothingToPublish(
            f"{collection.slug} @ {run.reference_time:%Y-%m-%dT%H:%MZ}: {len(times)} step(s) "
            f"span no period window at all. Instant values alone are a forecast with no "
            f"precipitation and no symbol."
        )

    return PublishPlan(
        publication=publication,
        run=run,
        reference_time=run.reference_time,
        run_version=run.version,
        generation=_generation_for(publication, run),
        times=times,
        parameters=chosen,
        hrefs={slug: [hrefs_by_variable[slug][time] for time in times] for slug in hrefs_by_variable},
        time_until_next=_time_until_next(publication, collection, run),
    )


def _generation_for(publication, run) -> int:
    """The generation this publish would carry, reset if the run has moved on.

    The generation counts changes to the *bytes* that are not changes to the run,
    so it belongs to a run and not to the publication for all time. It resets
    whenever the run being published is not the one it was last raised against —
    and it may reset *downwards* by 99, which is safe because the term above it
    has moved up: a new run at generation 0 outranks any generation of the run
    before it, which is what keeps D8's model-time-first ordering intact.

    Which run it was raised against is read back out of ``published_version``
    rather than kept in a column of its own. A second column would be a second
    authority on one fact, and the pair would eventually disagree — most likely
    at exactly the moment a publish died between the two writes.

    Refuses at the ceiling rather than wrapping. ``GENERATION_CEILING`` is not a
    field width: the generation occupies the same two digits the run's revision
    would shift into, so generation 100 *is* the stamp ``revision + 1`` produces
    at generation 0. Two different sets of bytes would then claim one integer,
    and the reader — reloading on strictly greater — would read the second as one
    it already holds. Silent, and indistinguishable from a healthy instance.
    """
    if publication.published_run_version == run.version:
        generation = publication.generation
    else:
        generation = 0

    if generation >= GENERATION_CEILING:
        raise PublicationRefused(
            f"{publication.slug} is at generation {generation}, which is the ceiling. "
            f"The generation is the low two digits of the version, so publishing here "
            f"would produce {run.version * GENERATION_CEILING + generation} — the same "
            f"integer this run claims at revision {run.revision + 1}, generation 0. The "
            f"reader reloads only on a strictly greater version and would read the "
            f"second set of bytes as one it already holds. Wait for the next run, which "
            f"resets the generation, or publish a second model."
        )
    return generation


def _refuse_unpublishable_collection(collection) -> None:
    if collection.visibility != collection.Visibility.PUBLIC:
        raise PublicationRefused(
            f"{collection.slug} is {collection.visibility}, not public. A Forti reader "
            f"presents no credential, so there is nobody to check a restricted "
            f"collection against."
        )


def _resolve_variables(collection, needed: set[str]) -> dict:
    """The Variable rows behind the slugs, with their units checked.

    Forti copies units out of ``meta.json`` without interpreting them, so a
    variable retuned from ``degC`` to ``K`` publishes a number wrong by 273 under
    a label that says celsius, and every layer downstream agrees.
    """
    found = {variable.slug: variable for variable in collection.variables.filter(slug__in=needed)}

    missing = sorted(needed - set(found))
    if missing:
        raise PublicationRefused(
            f"{collection.slug} is missing variable(s) {', '.join(missing)}. The parameter "
            f"map needs all of {', '.join(sorted(needed))}."
        )

    expected = params.expected_units()
    wrong = []
    for slug, variable in found.items():
        want = expected.get(slug)
        have = variable.unit.symbol if variable.unit else None
        if want and not _same_unit(have, want):
            wrong.append(f"{slug} is {have!r}, expected {want!r}")
    if wrong:
        raise PublicationRefused(
            "Units do not match what the parameter map publishes: "
            + "; ".join(wrong)
            + ". Forti reads units from meta.json without converting, so this would "
            "publish a wrong number under a right-looking label."
        )

    return found


def _same_unit(symbol: str | None, expected: str) -> bool:
    """Whether two unit symbols name the same unit, not merely a compatible one.

    Compared through pint rather than as strings: an instance may spell degrees
    Celsius ``°C`` or ``degC`` depending on which seed wrote the row, and both are
    the same unit. Kelvin is *compatible* with Celsius and is not the same unit —
    which is the whole point of checking, since Forti converts nothing.

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


def _cog_hrefs(collection, reference_time, variables) -> dict:
    """``{variable_slug: {time: href}}`` for one run's COG assets."""
    assets = (
        Asset.objects.filter(
            item__collection=collection,
            item__reference_time=reference_time,
            variable__in=variables.values(),
            format=Asset.Format.COG,
        )
        .select_related("variable")
        .values_list("variable__slug", "item__time", "href")
    )

    by_variable = {slug: {} for slug in variables}
    for slug, time, href in assets:
        by_variable[slug][time] = href
    return by_variable


def _shared_times(hrefs_by_variable) -> list:
    """The timesteps every variable has, in order."""
    if not hrefs_by_variable:
        return []
    shared = set.intersection(*(set(times) for times in hrefs_by_variable.values()))
    return sorted(shared)


def _time_until_next(publication, collection, run) -> timedelta | None:
    """How long until the next run is expected, for ``complete.json``.

    An explicit override wins. Otherwise it is the gap between the last two
    closed runs, which is a measurement rather than a guess — a feed reconfigured
    from 12-hourly to 6-hourly reports the truth on its second run without
    anybody editing anything. With only one closed run there is nothing to
    measure and the field is omitted.
    """
    if publication.time_until_next_hours:
        return timedelta(hours=publication.time_until_next_hours)

    previous = (
        RunIngestion.objects.filter(
            collection=collection,
            status=RunIngestion.Status.CLOSED,
            reference_time__lt=run.reference_time,
        )
        .order_by("-reference_time")
        .first()
    )
    if previous is None:
        return None
    return run.reference_time - previous.reference_time


def latest_item_geometry(collection, reference_time):
    """One Item of the run, to read the raster geometry from.

    The grid comes from the collection's raster geometry and the publication's
    bbox, so any item of the run will do — they all share a transform.
    """
    return Item.objects.filter(collection=collection, reference_time=reference_time).order_by("time").first()
