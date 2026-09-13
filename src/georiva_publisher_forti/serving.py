"""``GET /api/forecast/{model}/?lat=&lon=`` — one model's point forecast.

The document returned is met.no's **locationforecast 2.0**, produced by
``jsonfrontend`` and passed through unchanged. GeoRiva adds nothing to it and
knows nothing about any particular consumer (D1); the published document is the
whole contract. ``GET /api/forecast/`` lists the models the caller may ask for.

**Why Django fronts this at all.** The machine plane's ``auth_request`` gate
cannot be reused: ``scope_of`` (`core/machine_plane/addresses.py:129`) recognises
only ``/titiler/`` and ``/martin/boundary_stats`` and returns ``None`` for
everything else, which the caller treats as deny. Rather than widen a function
whose deny-by-default shape is the point, D11 puts a view in front and keeps the
Go services off the network entirely — neither publishes a port.

**Why the view lives in the plugin.** D19: core holds generic mechanisms and a
plugin holds everything shaped like its own domain. The route reaches ``/api/``
through core's plugin URL hook (ADR 0028 in core) and nothing about a point
forecast is written down in core — not the upstream's name, not the timeout, not
the throttle rate, not the cache lifetime. Every setting below is read with a
default that lives here, which is the pattern ``config.storage_settings()``
already follows.

**The model is the resource, and it is also the tenant boundary.** An earlier
draft of this view derived the upstream's name from the organisation slug and
said *"that derivation is also the tenant boundary … isolation is which process
is asked, and nothing else"*. D14/D16 reverse it. There is one pair for the
instance and one fixed service name; the boundary is what the request names,
resolved by a Django query and checked on **every response** by asserting that
``properties.meta.area`` is the area we asked for. What that buys and what it
gives up is written down in `docs/adr/0001-the-model-is-the-public-resource.md`.

**The sharp edge is the empty area list.** Omitting ``area`` upstream does not
mean "no areas", it means *every organisation's areas* — ``bestArea``
(`forecast.go:129`) answers from the nearest gridpoint among everything the
process holds, and an empty filter narrows nothing. So there is exactly one
place a request body is built, :func:`upstream_params`, and it raises before the
socket rather than letting an empty area reach it.
"""

import logging

import requests
from django.conf import settings
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from georiva.organisations.access import require_active_org, scoped_queryset

from . import parameters as params
from .models import FortiPublication

logger = logging.getLogger(__name__)

#: Latitude and longitude bounds. The same range ``getParam``
#: (`jsonfrontend/internal/server/server.go`) enforces, checked here so an
#: obviously bad request costs nothing upstream — not because jsonfrontend would
#: mishandle it.
_LAT_RANGE = (-90.0, 90.0)
_LON_RANGE = (-180.0, 180.0)

#: The service name the pair publishes under, from the plugin's own
#: `deploy/compose.yml`. Fixed rather than derived: there is one pair for the
#: instance (D14), so there is no slug in it and no registry to drift from.
DEFAULT_JSONFRONTEND_HOST = "forti-jsonfrontend"
DEFAULT_JSONFRONTEND_PORT = 8080

#: Seconds. jsonfrontend gives its own upstream call 1500 ms
#: (`server.go`, ``context.WithTimeout``), so a longer wait here only holds a
#: worker open past the point the answer can arrive.
DEFAULT_TIMEOUT = 5

#: The throttle, per client per organisation. There is no ``DEFAULT_THROTTLE_RATES``
#: in core's ``REST_FRAMEWORK`` — this is the first throttled route on the
#: instance — and D19 forbids adding one, so the rate is the plugin's; see
#: :class:`ForecastThrottle`.
DEFAULT_THROTTLE_RATE = "60/min"

#: Seconds a *public* model's response may be shared. §6 records met.no's
#: production ``data_expiry_offset`` as 1800 and that is where this number comes
#: from, but it is deliberately **not** :data:`jsonformat.DATA_EXPIRY_OFFSET`:
#: that one is "seconds after a run's last timestep at which a response stops
#: being served", a different quantity that happens to have been given the same
#: value. Tying them together would mean tuning an expiry hint silently changed
#: how long a proxy holds a document.
#:
#: The data behind it changes once per run — six hours at the fastest — so half
#: an hour of sharing costs a consumer nothing they would notice and takes the
#: hot path off Django entirely.
DEFAULT_MAX_AGE = 1800


def upstream_url() -> str:
    """Where ``jsonfrontend`` answers.

    Overridable because a deployment that does not use the plugin's compose file
    still has to be able to say where the pair is; not overridable *in core*,
    which is D19.
    """
    host = getattr(settings, "GEORIVA_FORTI_JSONFRONTEND_HOST", DEFAULT_JSONFRONTEND_HOST)
    port = getattr(settings, "GEORIVA_FORTI_JSONFRONTEND_PORT", DEFAULT_JSONFRONTEND_PORT)
    return f"http://{host}:{port}/"


class ForecastThrottle(SimpleRateThrottle):
    """Rate limit per client **and** per organisation, at a rate this plugin owns.

    Two things here are not DRF's default, and each replaces something that was
    quietly broken.

    **The rate comes from a plugin setting.** The obvious base class is
    ``ScopedRateThrottle``, and an earlier draft used it — but its whole
    mechanism is to look the rate up in ``settings.REST_FRAMEWORK
    ["DEFAULT_THROTTLE_RATES"][scope]``, and core has no ``DEFAULT_THROTTLE_RATES``
    at all. ``get_rate()`` raises ``ImproperlyConfigured`` on a missing scope, so
    the first request that reached the counter would have been a 500. Adding the
    key to core's settings is what that draft did and what D19 forbids. Reading
    it here instead keeps the whole route — view, URL, throttle, upstream — inside
    the plugin, and it is the same ``getattr(settings, "GEORIVA_FORTI_…",
    <plugin default>)`` shape as everything else in this package.

    ``None`` turns the throttle off: ``SimpleRateThrottle.allow_request`` returns
    ``True`` on a null rate, which is the honest way for an operator behind their
    own rate limiter to say so.

    **The scope is a class attribute, not a view attribute.** That is also why
    the base class changed. ``ScopedRateThrottle.allow_request`` overwrites
    ``self.scope`` with ``getattr(view, "throttle_scope", None)`` and **allows
    the request unconditionally when it is unset** — so the earlier draft, whose
    view declared no ``throttle_scope``, never throttled anything, while its
    unit test passed because it constructed a fake view that had the attribute.
    A throttle that silently does nothing is worse than none: it is a control
    somebody believes is there.

    **The organisation is part of the key.** ``SimpleRateThrottle`` keys
    anonymous callers on IP alone. On a multi-tenant instance that puts two
    organisations' callers behind one NAT into the same bucket, so one tenant's
    traffic could exhaust another's allowance. The organisation is part of the
    key for the same reason it is part of the answer.
    """

    scope = "georiva_forti_forecast"

    def get_rate(self):
        return getattr(settings, "GEORIVA_FORTI_FORECAST_THROTTLE_RATE", DEFAULT_THROTTLE_RATE)

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            ident = f"user:{user.pk}"
        else:
            ident = f"ip:{self.get_ident(request)}"

        # The host is what decides the organisation (``OrganisationMiddleware``
        # resolves Host → Site → Organisation), and every route below has
        # already required one, so ``none`` is only reachable from a bare
        # throttle instance in a test.
        organisation = getattr(request, "active_org", None)
        slug = organisation.slug if organisation is not None else "none"
        return self.cache_format % {"scope": self.scope, "ident": f"{slug}:{ident}"}


def servable_models(request):
    """The models ``request`` may be served. The **one** query both routes ask.

    D18's rule has two halves that have to agree: a model the caller may not see
    is absent from the listing *and* 404s on its own URL, so the endpoint cannot
    be used to enumerate what a tenant publishes. Two filters would be two things
    to keep in step, and the drift is silent in the direction that matters — a
    model missing from a listing that still answers when named.

    Tenancy and audience are separate filters on purpose (ADR 0011): the
    organisation comes from the host, through ``scoped_queryset``, and stays
    visible at the call site rather than hiding inside the queryset method.
    """
    return scoped_queryset(request, FortiPublication.objects.visible_to(request))


def upstream_params(area: str, latitude: float, longitude: float) -> dict:
    """The complete query for one point of one area. The only one built anywhere.

    **An absent ``area`` is every organisation's areas.** proto3 decodes an
    absent repeated field to a nil slice and ``selects`` (`forecast.go`) reads
    an empty filter as "no narrowing at all", so a request that forgets the area
    is not a request for nothing — it is a request answered from whichever
    tenant's gridpoint happens to be nearest, as arithmetic, with nothing
    reporting it. The area key cannot be empty (it is ``{org}.{slug}`` and both
    halves are validated non-empty), which is exactly why the check is here
    rather than in the caller: it costs nothing and it is what makes the failure
    unreachable rather than unlikely.

    It raises rather than returning an error, and nothing catches it. A missing
    area is an instance bug and a 500 is the honest report of one; a 4xx would
    file it under "the caller did something wrong".

    Nothing else is forwarded. An allowlist rather than a passthrough for the
    same reason: the parameters that would matter are the ones that change which
    data answers, and the two that do — ``area`` and ``topography`` — are
    refused at the route with a 400 rather than dropped, because a caller who
    sent one believes it did something.
    """
    if not area:
        raise RuntimeError(
            "refusing to ask jsonfrontend for a forecast with no area: an omitted "
            "area is not 'no areas', it is every organisation's areas."
        )
    return {"lat": latitude, "lon": longitude, "area": area}


def ask_upstream(area: str, latitude: float, longitude: float):
    """One HTTP call to ``jsonfrontend``. Raises ``requests.RequestException``."""
    return requests.get(
        upstream_url(),
        params=upstream_params(area, latitude, longitude),
        timeout=getattr(settings, "GEORIVA_FORTI_TIMEOUT", DEFAULT_TIMEOUT),
        # Not forwarded: jsonfrontend offers gzip, and asking for it here would
        # only mean decompressing a body we immediately re-serve.
        headers={"Accept-Encoding": "identity"},
    )


def area_objection(document, expected: str) -> str | None:
    """Why ``document`` may not be served as ``expected``, or ``None``.

    The tenant boundary, checked on every response. ``rawdataforecaster`` reports
    the answering area back as the key it holds the dataset under
    (`forecast.go`, ``bestDataset.Area``) and jsonfrontend copies it to
    ``properties.meta.area`` (`encode.go:100`), so the name a caller audits is
    the name the request asked for. **Equality, not membership** (D16): a
    request names exactly one area, so anything else answering is a fault
    whatever it is.

    The one response that legitimately names no area is the coverage error.
    ``EncodeError`` (`encode.go:30`) builds its metadata from scratch — an
    ``error`` string, an empty ``units`` map and an empty ``timeseries`` — and
    never sets ``Area``, because there is no dataset to attribute. It carries no
    forecast data, so nothing can leak through it; every *other* shape of
    missing area is refused.
    """
    if not isinstance(document, dict):
        return "the response is not a JSON object"

    properties = document.get("properties")
    if not isinstance(properties, dict):
        return "the response carries no properties"

    meta = properties.get("meta")
    if not isinstance(meta, dict):
        return "the response carries no properties.meta"

    answered = meta.get("area") or ""
    if answered == expected:
        return None
    if answered:
        return f"it was answered by {answered!r}"
    if properties.get("timeseries"):
        return "it names no area and carries a timeseries"
    if meta.get("error"):
        # The coverage error: no dataset answered, so there is no area to name.
        return None
    return "it names no area"


def describe(publication) -> dict:
    """One model, as a consumer picking between them needs to see it.

    D17 says the listing returns "the models the caller may see" and no more, so
    what is here is what somebody choosing a model or checking coverage has to
    know, and nothing that describes how the instance is run. Absent on purpose:
    the version, the grid id, the point and step counts, the build status, the
    collection and catalog slugs and the owning organisation. Those answer "is
    the pipeline healthy", which is M5.8's read-only panel and an operator's
    question, not a consumer's.

    ``title`` is the **catalog's** name because the slug is the catalog's slug:
    D17's reason for prefilling from it is that ``Catalog`` is already the thing
    a consumer would call a model — *"a data source that produces multiple
    collections. Examples: GFS, CHIRPS, ERA5, MSG"* — so the name and the name
    it is asked by come off the same object.

    ``parameters`` is translated into locationforecast's vocabulary rather than
    reported as stored. ``published_parameters`` holds forti's *internal* names
    (``air_temperature_2m``), which appear nowhere in the document a consumer
    reads; publishing them here would advertise a third vocabulary that answers
    no question. An unrecognised name is dropped rather than guessed at — it can
    only come from a row published by a version of this plugin that knew a
    parameter this one does not.
    """
    published = []
    for name in publication.published_parameters or []:
        parameter = params.BY_NAME.get(name)
        if parameter is not None:
            published.append(parameter.locationforecast)

    return {
        "model": publication.slug,
        "title": publication.collection.catalog.name,
        "visibility": publication.effective_visibility,
        "bbox": list(publication.bbox),
        "reference_time": publication.published_reference_time,
        "parameters": sorted(set(published)),
    }


def _no_store(payload, status_code):
    """A response nginx must not keep, said out loud rather than left implied.

    The ``/api/`` zone has no ``proxy_cache_valid`` (ADR 0029), so an unmarked
    response is already not stored and this header changes nothing there. It is
    written anyway because the zone is not the only cache between here and a
    consumer, and because an error that says nothing about its own lifetime is
    one somebody else's CDN is entitled to guess about.
    """
    response = Response(payload, status=status_code)
    response["Cache-Control"] = "no-store"
    return response


def _document(document, publication):
    """A forecast, marked with who may share it.

    The ``/api/`` cache key is ``$host$request_uri`` and **carries no identity**,
    so ``public`` means "serve this to every caller on this host" and nothing
    less. That is true of a public model's forecast — it is the same document
    for everybody, until the next run — and it is the whole reason the marking
    is allowed to exist at all.

    It is therefore keyed on :attr:`FortiPublication.effective_visibility`, the
    narrower of the publication's tier and the collection's, rather than on the
    stored field: a collection narrowed to private after its model was published
    public must stop being shared, and it is the *read* that has to notice,
    because nothing re-validates the publication when the collection is edited.

    A private model's answer is served to the member who asked and to nobody
    else, so it says so twice — ``private`` for anything that keys on identity,
    ``no-store`` for anything that does not.
    """
    response = Response(document, status=status.HTTP_200_OK)
    if publication.effective_visibility == FortiPublication.Visibility.PUBLIC:
        max_age = getattr(settings, "GEORIVA_FORTI_FORECAST_MAX_AGE", DEFAULT_MAX_AGE)
        response["Cache-Control"] = f"public, max-age={max_age}"
    else:
        response["Cache-Control"] = "private, no-store"
    return response


def _coordinate(raw, name, bounds):
    if raw is None:
        raise ValueError(f"'{name}' is required.")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"'{name}' must be a number.") from None
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"'{name}' must be between {low} and {high}.")
    return value


class ForecastModelListView(APIView):
    """``GET /api/forecast/`` — the models this caller may ask for.

    Never marked publicly cacheable, even though most of its callers are
    anonymous and would get a document that is safe to share. The response
    varies by audience at a URL that carries no audience, and nginx's
    ``proxy_no_cache`` on the session cookie is what would keep a member's
    listing out of the shared cache. That is precisely the reasoning ADR 0029
    rejects — correctness resting on having enumerated every way a credential
    can arrive — and the saving is a queryset, so there is nothing to buy with
    the risk.
    """

    throttle_classes = [ForecastThrottle]

    def get(self, request):
        require_active_org(request)
        publications = servable_models(request).select_related("collection", "collection__catalog")
        return _no_store({"models": [describe(publication) for publication in publications]}, status.HTTP_200_OK)


class ForecastView(APIView):
    """``GET /api/forecast/{model}/?lat=&lon=`` — one point out of one model.

    Deliberately thin about the *data*. Which gridpoint answers, how far is too
    far, which parameters exist — each was decided when the area was published
    or is decided by the reader, and duplicating any of it here would give the
    instance two answers that could disagree.

    Deliberately not thin about *which area answers*, which is the half no
    reader can check for us.
    """

    throttle_classes = [ForecastThrottle]

    def get(self, request, model):
        require_active_org(request)

        publication = servable_models(request).filter(slug=model).first()
        if publication is None:
            # Absent, not forbidden — D18. A caller who may not see a model must
            # not be able to tell it apart from one that does not exist, or the
            # route enumerates what a tenant publishes.
            raise Http404("No such forecast model.")

        # Resolved before the query is validated so that an unreadable model is
        # a 404 whatever else the request got wrong.
        refused = {name for name in ("area", "topography") if name in request.query_params}
        if refused:
            # Refused rather than ignored. Both change which data answers:
            # `area` is the tenant boundary this route sets itself, and
            # `topography` selects a set the caller cannot know the membership
            # of. Silently dropping either would leave a caller believing a
            # filter applied.
            return _no_store(
                {
                    "detail": (
                        f"{', '.join(sorted(refused))} may not be set here. The model in the "
                        f"path selects the data; there is nothing else to choose."
                    )
                },
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            latitude = _coordinate(request.query_params.get("lat"), "lat", _LAT_RANGE)
            longitude = _coordinate(request.query_params.get("lon"), "lon", _LON_RANGE)
        except ValueError as exc:
            return _no_store({"detail": str(exc)}, status.HTTP_400_BAD_REQUEST)

        area = publication.area_key
        try:
            upstream = ask_upstream(area, latitude, longitude)
        except requests.RequestException as exc:
            # No pair running, or one that is down. Both are "no point forecast
            # is being served here", which is not something the caller can fix
            # and not their fault.
            logger.warning("forecast: upstream unreachable for %s: %s", area, exc)
            return _no_store(
                {"detail": "The point forecast service is temporarily unavailable."},
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Every branch below switches on the status code and on whether the body
        # parses — **never on Content-Type**. jsonfrontend sets no content type
        # on a successful response, so Go's sniffing stamps
        # `text/plain; charset=utf-8` over a perfectly good locationforecast
        # document; a view that trusted the header would send every real
        # forecast down an error branch. Its failures are `http.Error` prose,
        # which does not parse, and that is the distinction that works.
        if upstream.status_code >= 500:
            logger.warning("forecast: upstream %s for %s", upstream.status_code, area)
            return _no_store(
                {"detail": "The point forecast service is temporarily unavailable."},
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if upstream.status_code == 404:
            # "Outside of coverage area" — and it can only mean the area we named
            # is not loaded. The other route to that 404, a point outside the
            # dataset's own polygon, is unreachable: GeoRiva writes no
            # `geographic_extent`, so `WithinGeographicArea` (`dataset.go:196`)
            # returns true unconditionally. `bestArea` fails closed when every
            # named area is missing, which is exactly the state M5.9 has to clear
            # — a row whose `published_version` is set under a layout whose bytes
            # are not on the bucket. Passing the 404 through would blame the
            # caller's coordinates for a model that is simply not resident.
            logger.error(
                "forecast: %s is served by GeoRiva but not loaded by rawdataforecaster (upstream 404: %s)",
                area,
                upstream.text.strip(),
            )
            return _no_store(
                {"detail": "This model is not currently being served."},
                status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if upstream.status_code != 200:
            # There is no caller input that can reach this. The coordinates are
            # validated against jsonfrontend's own bounds and every other
            # parameter is ours, so a 4xx here means we built a request the
            # reader would not take — most sharply, an `area=` it read as empty.
            logger.error(
                "forecast: jsonfrontend refused our own request for %s with %s: %s",
                area,
                upstream.status_code,
                upstream.text.strip(),
            )
            return _no_store(
                {"detail": "The point forecast service returned an unexpected response."},
                status.HTTP_502_BAD_GATEWAY,
            )

        try:
            document = upstream.json()
        except ValueError:
            logger.error("forecast: a 200 from jsonfrontend for %s did not parse as JSON", area)
            return _no_store(
                {"detail": "The point forecast service returned an unexpected response."},
                status.HTTP_502_BAD_GATEWAY,
            )

        objection = area_objection(document, area)
        if objection is not None:
            # The one check that catches a view bug before a consumer does. It
            # does *not* catch a publisher bug that wrote one organisation's data
            # under another's key — that passes silently, and saying so is part
            # of what D14 gives up.
            logger.error("forecast: refusing a document asked of %s because %s", area, objection)
            return _no_store(
                {"detail": "The point forecast service returned an unexpected response."},
                status.HTTP_502_BAD_GATEWAY,
            )

        # Passed through untouched. That includes the 200-carrying-an-error-body
        # jsonfrontend returns for a point inside the grid but further than
        # `maximum_gridpoint_distance` from any of its gridpoints, which is how
        # "outside coverage" arrives for a model that *is* loaded. It is a
        # stable, public fact about that coordinate, so it is marked like any
        # other successful answer.
        return _document(document, publication)
