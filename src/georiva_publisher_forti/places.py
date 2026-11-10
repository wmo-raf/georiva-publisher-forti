"""``GET /api/forecast/{model}/places/`` — one model's forecast, told for its towns.

The point route answers one coordinate; a map of towns wants every town at once.
This route joins the two: the towns of core's gazetteer inside the model's area,
each asked of ``jsonfrontend`` exactly as ``/api/forecast/{model}/`` would ask
it, collected into one **matrix** — towns × valid times — per published version.

**Built lazily, once per version.** One reader call returns a town's whole
series, so the expensive part is one call per town per published run, and every
request after that is a slice. The first request after a publish builds it under
a single-flight lock, with bounded concurrency; the matrix is cached under
``(model, version, towns digest)``, so a new run or an edited gazetteer is a new
key and nothing has to be invalidated.

**Never built from the wrong version.** The database's ``published_version``
moves a moment before the reader loads it, and a matrix built in that moment
would be the old run cached under the new run's key until the run after. So a
build waits until the reader's status document reports the published version
resident (:func:`verification.resident_areas`), and is thrown away if the
reader reloads under it — every answer must carry the same ``updated_at``, the
reader's load stamp. Neither failure is cached; the caller is told to retry.

**Which towns.** The instance-wide towns plus the *owning* organisation's own,
inside the publication's bbox — whoever asks. The model is the owner's product
and their towns are part of how they publish it; a per-requester set would make
the matrix per requester and let one organisation's towns query another's model.
A town the reader cannot answer for (a masked cell, too far from a gridpoint) is
dropped rather than shown empty.

**Which window.** By default the finest the model publishes — a 1-hourly model's
``next_1_hours``, a 3-hourly model's ``next_6_hours`` — so a symbol describes
the same hour the map under it shows. ``window=`` picks another the model has.
"""

import hashlib
import time as clock
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import requests
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from georiva.organisations.access import require_active_org

from . import serving
from .forti_settings import read
from .verification import resident_areas

#: ISO 8601 duration → the locationforecast block that carries it, finest first.
WINDOWS = {"PT1H": "next_1_hours", "PT6H": "next_6_hours", "PT12H": "next_12_hours"}


def window_hours(window: str) -> int:
    """``PT6H`` → 6."""
    return int(window.removeprefix("PT").removesuffix("H"))


#: What each town carries, per window: instants read at the window's start.
INSTANTS = ("air_temperature", "wind_speed", "wind_from_direction")

DEFAULT_CONCURRENCY = 8
DEFAULT_WAIT = 5
#: Seconds a matrix is kept. It is keyed by version, so this only bounds memory:
#: a superseded version's matrix is never read again and ages out.
MATRIX_TTL = 24 * 3600
LOCK_TTL = 120
RETRY_AFTER = "5"

#: Seconds a *public* answer may be shared. Much shorter than the point route's:
#: this URL names no version, so whatever a cache holds is what a map sees, and
#: a long share would keep a new run's symbols behind the last run's. The ETag
#: makes the revalidation after it cheap.
PUBLIC_MAX_AGE = 60

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class NotReady(Exception):
    """The matrix cannot be built yet; the caller should retry shortly."""


class Unavailable(Exception):
    """The reader is down or answered something we will not serve."""

    def __init__(self, detail, status_code):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Towns
# ---------------------------------------------------------------------------


def towns_for(publication):
    """The towns a model is told for: instance-wide plus the owner's, in its bbox.

    The organisation filter is written out rather than scoped through the
    request on purpose: the set is the *publication owner's*, the same for every
    caller (see the module docstring), so there is no request to scope by.

    ``Town`` is imported here rather than at the top: it is newer than the core
    this plugin otherwise supports, and a failed import of this module would
    make core skip *all* of ``api_urls`` — the point route with it.
    """
    from georiva.core.models import Town

    west, south, east, north = publication.bbox
    return list(
        Town.objects.filter(
            Q(organisation__isnull=True) | Q(organisation=publication.organisation),
            is_active=True,
            latitude__gte=south,
            latitude__lte=north,
            longitude__gte=west,
            longitude__lte=east,
        ).order_by("rank", "-population", "name", "pk")
    )


def towns_digest(towns) -> str:
    """Changes whenever anything a matrix shows about its towns does."""
    h = hashlib.sha256()
    for t in towns:
        h.update(f"{t.pk}|{t.name}|{t.latitude}|{t.longitude}|{t.rank}|{t.population}\n".encode())
    return h.hexdigest()[:16]


def matrix_key(publication, digest) -> str:
    return f"forti:places:{publication.pk}:{publication.published_version}:{digest}"


def lock_key(publication) -> str:
    return f"forti:places:lock:{publication.pk}:{publication.published_version}"


# ---------------------------------------------------------------------------
# Building the matrix
# ---------------------------------------------------------------------------


def _ask(area, town):
    """One town's document, or ``None`` where the reader has no forecast for it."""
    try:
        upstream = serving.ask_upstream(area, town.latitude, town.longitude)
    except requests.RequestException as exc:
        raise Unavailable("The point forecast service is temporarily unavailable.", 503) from exc

    if upstream.status_code >= 500 or upstream.status_code == 404:
        raise Unavailable("The point forecast service is temporarily unavailable.", 503)
    if upstream.status_code != 200:
        raise Unavailable("The point forecast service returned an unexpected response.", 502)
    try:
        doc = upstream.json()
    except ValueError:
        raise Unavailable("The point forecast service returned an unexpected response.", 502) from None

    objection = serving.area_objection(doc, area)
    if objection is not None:
        serving.logger.error("places: refusing a document asked of %s because %s", area, objection)
        raise Unavailable("The point forecast service returned an unexpected response.", 502)

    properties = doc["properties"]
    if not properties.get("timeseries"):
        return None  # the coverage error: no forecast at this town
    return properties


def build(publication, towns) -> dict:
    """Ask the reader for every town and fold the answers into one matrix.

    The matrix is stored, not served: every window is kept, and a response
    picks one. ``values[window][field][town][time]`` over the union of times.
    """
    area = publication.area_key
    workers = read("GEORIVA_FORTI_PLACES_CONCURRENCY", DEFAULT_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        answers = list(pool.map(lambda town: _ask(area, town), towns))

    kept = [(town, props) for town, props in zip(towns, answers, strict=True) if props is not None]
    stamps = {props["meta"].get("updated_at") for _, props in kept}
    if len(stamps) > 1:
        # The reader loaded a new version part-way through.
        raise NotReady("the reader reloaded during the build")

    times = sorted({entry["time"] for _, props in kept for entry in props["timeseries"]})
    index = {t: i for i, t in enumerate(times)}
    units = {}
    for _, props in kept:
        units.update(props["meta"].get("units") or {})

    def grid():
        return [[None] * len(times) for _ in kept]

    instants = {name: grid() for name in INSTANTS}
    windows = {w: {"symbol_code": grid(), "precipitation_amount": grid()} for w in WINDOWS}
    for row, (_, props) in enumerate(kept):
        for entry in props["timeseries"]:
            col = index[entry["time"]]
            data = entry.get("data") or {}
            details = (data.get("instant") or {}).get("details") or {}
            for name in INSTANTS:
                instants[name][row][col] = details.get(name)
            for window, block_name in WINDOWS.items():
                block = data.get(block_name)
                if not block:
                    continue
                windows[window]["symbol_code"][row][col] = (block.get("summary") or {}).get("symbol_code")
                windows[window]["precipitation_amount"][row][col] = (block.get("details") or {}).get(
                    "precipitation_amount"
                )

    reference_time = publication.published_reference_time
    return {
        "version": publication.published_version,
        "reference_time": reference_time.astimezone(UTC).strftime(TIME_FORMAT) if reference_time else None,
        "updated_at": next(iter(stamps), None),
        "units": units,
        "times": times,
        "towns": [
            {
                "id": town.pk,
                "name": town.name,
                "rank": town.rank,
                "population": town.population,
                "coordinates": [town.longitude, town.latitude],
            }
            for town, _ in kept
        ],
        "instants": instants,
        "windows": windows,
    }


def matrix_for(publication) -> tuple[dict, str]:
    """The cached matrix for the published version, building it if need be.

    Returns the matrix and the towns digest it was keyed under.
    """
    towns = towns_for(publication)
    digest = towns_digest(towns)
    key = matrix_key(publication, digest)
    cached = cache.get(key)
    if cached is not None:
        return cached, digest

    if not cache.add(lock_key(publication), 1, timeout=LOCK_TTL):
        # Somebody else is building this version. Wait for theirs.
        deadline = clock.monotonic() + read("GEORIVA_FORTI_PLACES_WAIT", DEFAULT_WAIT)
        while clock.monotonic() < deadline:
            clock.sleep(0.25)
            cached = cache.get(key)
            if cached is not None:
                return cached, digest
        raise NotReady("another request is building this model's towns")

    try:
        residency = resident_areas().of(publication)
        if residency.loaded != publication.published_version:
            raise NotReady("the reader has not loaded the published version yet")
        matrix = build(publication, towns)
        # The gate is checked again on the far side: had the reader moved on
        # to the next run before the first town was asked, every town would
        # answer from it with one stamp, and the matrix would be the next run
        # cached under this one's key.
        if resident_areas().of(publication).loaded != publication.published_version:
            raise NotReady("the reader loaded another version during the build")
        cache.set(key, matrix, timeout=MATRIX_TTL)
        return matrix, digest
    finally:
        cache.delete(lock_key(publication))


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def available_windows(matrix) -> list[str]:
    return [
        window
        for window in WINDOWS
        if any(code is not None for row in matrix["windows"][window]["symbol_code"] for code in row)
    ]


def answerable(matrix, window) -> list[int]:
    """Indexes of the times at which at least one town has a symbol for ``window``."""
    codes = matrix["windows"][window]["symbol_code"]
    return [col for col in range(len(matrix["times"])) if any(row[col] is not None for row in codes)]


def _values(matrix, window, cols):
    pick = lambda grid: [[row[c] for c in cols] for row in grid]  # noqa: E731
    return {
        "symbol_code": pick(matrix["windows"][window]["symbol_code"]),
        **{name: pick(matrix["instants"][name]) for name in INSTANTS},
        "precipitation_amount": pick(matrix["windows"][window]["precipitation_amount"]),
    }


def whole_run(publication, matrix, window) -> dict:
    cols = answerable(matrix, window)
    return {
        "model": publication.slug,
        "reference_time": matrix["reference_time"],
        # The area the towns were chosen from, west/south/east/north: what a
        # map frames to show them.
        "bbox": list(publication.bbox),
        "window": {"duration": window},
        "times": [matrix["times"][c] for c in cols],
        "units": matrix["units"],
        "towns": matrix["towns"],
        "values": _values(matrix, window, cols),
    }


def one_time(publication, matrix, window, when) -> dict:
    cols = answerable(matrix, window)
    times = [matrix["times"][c] for c in cols]
    if when not in times:
        raise Http404("This model has no town forecast for that time and window.")
    col = matrix["times"].index(when)
    values = _values(matrix, window, [col])
    start = datetime.strptime(when, TIME_FORMAT).replace(tzinfo=UTC)
    end = start + timedelta(hours=window_hours(window))
    features = []
    for row, town in enumerate(matrix["towns"]):
        properties = {
            "id": town["id"],
            "name": town["name"],
            "rank": town["rank"],
            "population": town["population"],
        }
        properties.update({name: values[name][row][0] for name in values})
        if properties["symbol_code"] is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": town["coordinates"]},
                "properties": properties,
            }
        )
    return {
        "type": "FeatureCollection",
        "model": publication.slug,
        "reference_time": matrix["reference_time"],
        # The area the towns were chosen from, west/south/east/north: what a
        # map frames to show them.
        "bbox": list(publication.bbox),
        "time": when,
        "window": {"duration": window, "start": when, "end": end.strftime(TIME_FORMAT)},
        "times": times,
        "units": matrix["units"],
        "features": features,
    }


def parse_time(raw: str) -> str:
    """An ISO 8601 instant, spelled the way the reader spells times."""
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("'time' must be an ISO 8601 instant, e.g. 2026-09-02T18:00:00Z.") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime(TIME_FORMAT)


def _etag_matches(header: str | None, etag: str) -> bool:
    """``If-None-Match`` against ``etag``, weakly: nginx's gzip turns ``"x"`` into ``W/"x"``."""
    if not header:
        return False
    return any(tag.strip().removeprefix("W/") in (etag, "*") for tag in header.split(","))


def _marked(payload, publication, status_code=status.HTTP_200_OK):
    """Marked like the point route, but shared for :data:`PUBLIC_MAX_AGE` at most."""
    response = serving._document(payload, publication)
    response.status_code = status_code
    if publication.effective_visibility == publication.Visibility.PUBLIC:
        response["Cache-Control"] = f"public, max-age={PUBLIC_MAX_AGE}"
    return response


def _not_ready(detail):
    response = Response({"detail": detail}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response["Cache-Control"] = "no-store"
    response["Retry-After"] = RETRY_AFTER
    return response


class PlacesView(APIView):
    """``GET /api/forecast/{model}/places/[?time=][&window=]``.

    No ``time``: the whole run as arrays, ``values[field][town][time]`` — what a
    map that plays through a run fetches once. ``time``: one GeoJSON
    FeatureCollection, for anything that wants towns with a forecast as plain
    GeoJSON. Both read one matrix; both are marked like the point route but
    shared for a minute at most (:data:`PUBLIC_MAX_AGE`), and carry an ETag of
    the version, the towns digest and the window.
    """

    throttle_classes = [serving.ForecastThrottle]

    def get(self, request, model):
        require_active_org(request)
        publication = (
            serving.servable_models(request)
            .select_related("collection", "collection__catalog__organisation")
            .filter(slug=model, published_version__isnull=False)
            .first()
        )
        if publication is None:
            raise Http404("No such forecast model.")

        raw_window = request.query_params.get("window")
        if raw_window is not None and raw_window not in WINDOWS:
            return serving._no_store(
                {"detail": f"'window' must be one of {', '.join(WINDOWS)}."}, status.HTTP_400_BAD_REQUEST
            )
        raw_time = request.query_params.get("time")
        when = None
        if raw_time is not None:
            try:
                when = parse_time(raw_time)
            except ValueError as exc:
                return serving._no_store({"detail": str(exc)}, status.HTTP_400_BAD_REQUEST)

        try:
            matrix, digest = matrix_for(publication)
        except NotReady as exc:
            serving.logger.info("places: %s not ready: %s", publication.area_key, exc)
            return _not_ready("This model's town forecasts are being prepared. Retry shortly.")
        except Unavailable as exc:
            return serving._no_store({"detail": exc.detail}, exc.status_code)

        windows = available_windows(matrix)
        if not windows:
            return serving._no_store({"detail": "This model publishes no weather symbols."}, status.HTTP_404_NOT_FOUND)
        window = raw_window or windows[0]
        if window not in windows:
            return serving._no_store(
                {"detail": f"This model publishes the {', '.join(windows)} window(s), not {window}."},
                status.HTTP_400_BAD_REQUEST,
            )

        # Built before the ETag is compared, so a time the window cannot answer
        # is a 404 whatever the caller holds.
        payload = (
            whole_run(publication, matrix, window) if when is None else one_time(publication, matrix, window, when)
        )
        etag = f'"{matrix["version"]}-{digest}-{window}"'
        if _etag_matches(request.headers.get("If-None-Match"), etag):
            # Marked exactly as the 200 it stands for would be.
            response = _marked(None, publication, status.HTTP_304_NOT_MODIFIED)
        else:
            response = _marked(payload, publication)
        response["ETag"] = etag
        return response
