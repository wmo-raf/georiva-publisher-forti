"""What ``/api/forecast/`` promises, and which area it is allowed to answer from.

Four properties here are the whole of the tenant boundary, and every one of them
fails *silently* if it is not asserted:

- exactly one area is named on every upstream call, because an omitted one is
  not "no areas" but every organisation's areas;
- the area that answers is checked for equality with the one asked for;
- a model the caller may not see is absent from the listing and 404s on its own
  URL, by the same query, so the route cannot enumerate what a tenant publishes;
- only a public model's answer is marked shareable, because the ``/api/`` cache
  key carries no identity (ADR 0029).

The other cluster is the upstream's shape, which is not what you would guess and
which an honest-looking fake would hide — see :class:`FakeResponse`.
"""

import json
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from georiva.core.models import Collection
from georiva.organisations.testing import (
    DEFAULT_TEST_ORG_SLUG,
    dial_org,
    join_org,
    make_organisation,
    org_host,
)
from georiva_publisher_forti import rdfconfig
from georiva_publisher_forti.models import FortiPublication
from georiva_publisher_forti.serving import area_objection, upstream_params

from .factories import REFERENCE_TIME, make_collection, make_publication

#: A locationforecast document, trimmed to the parts this view can affect. The
#: area is the one the fixtures below publish under.
AREA = f"{DEFAULT_TEST_ORG_SLUG}.ecmwf-ifs"

DOCUMENT = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [36.8172, -1.2864]},
    "properties": {
        "meta": {"updated_at": "2026-09-02T12:00:00Z", "area": AREA},
        "timeseries": [{"time": "2026-09-02T12:00:00Z"}],
    },
}

#: What jsonfrontend returns for a point inside the grid but further than
#: `maximum_gridpoint_distance` from any gridpoint. `EncodeError` (`encode.go:30`)
#: builds the metadata from scratch and never sets `Area`, so this is the one
#: successful response that legitimately names none.
COVERAGE_ERROR = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [0.0, 51.5]},
    "properties": {
        "meta": {"updated_at": "2026-09-02T12:00:00Z", "error": "no data at the given location", "units": {}},
        "timeseries": [],
    },
}


class FakeResponse:
    """Shaped like the real reader, which is not what you would guess.

    jsonfrontend sets no Content-Type on a successful response, so Go's sniffing
    stamps ``text/plain; charset=utf-8`` on a perfectly good locationforecast
    document — verified against the running service. A fake that labelled its
    JSON honestly would hide the one bug this seam actually has: a view that
    switched on the header would send every real forecast down an error branch.
    """

    def __init__(self, payload=None, status_code=200, text="", content_type="text/plain; charset=utf-8"):
        self._payload = payload
        self.status_code = status_code
        self.text = text if payload is None else json.dumps(payload)
        self.headers = {"Content-Type": content_type}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON object could be decoded")
        return self._payload


def publish(publication, *, parameters=("air_temperature_2m", "weather_symbol")):
    """Put a publication into the state a first successful publish leaves it in.

    ``published_version`` is the field every serving query keys on, because it is
    set only after ``latest/<area key>`` is on the bucket.
    """
    FortiPublication.objects.filter(pk=publication.pk).update(
        published_version=178895520014,
        published_reference_time=REFERENCE_TIME,
        published_step_count=9,
        published_parameters=list(parameters),
    )
    publication.refresh_from_db()
    return publication


def served_model(org_slug=DEFAULT_TEST_ORG_SLUG, *, visibility=None, collection_visibility=None):
    """One organisation's published, servable model."""
    collection = make_collection(org_slug=org_slug, visibility=collection_visibility)
    publication = make_publication(collection, slug="ecmwf-ifs", visibility=visibility or "")
    return publish(publication)


class ServingTestCase(TestCase):
    """A per-test cache, because every route here counts against the throttle.

    The default cache on this instance is the dev Redis, shared with the running
    application. Without this a suite of twenty requests from ``127.0.0.1``
    would spend one bucket and the later tests would start seeing 429s — and it
    would leave counters behind in a cache somebody else is using.
    """

    org_slug = DEFAULT_TEST_ORG_SLUG

    def setUp(self):
        override = override_settings(
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": self.id(),
                }
            }
        )
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        dial_org(self.client, self.org_slug)

    def forecast(self, model="ecmwf-ifs", **params):
        return self.client.get(f"/api/forecast/{model}/", params or {"lat": -1.2864, "lon": 36.8172})

    def listing(self):
        return self.client.get("/api/forecast/")


class TheDocumentTests(ServingTestCase):
    def setUp(self):
        super().setUp()
        self.publication = served_model()

    def test_the_document_is_returned_unchanged(self):
        """GeoRiva adds nothing to the contract (D1)."""
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)) as upstream:
            response = self.forecast()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), DOCUMENT)
        upstream.assert_called_once()

    def test_exactly_one_area_is_named_and_it_is_the_model_in_the_path(self):
        """The tenant boundary on the way out. `bestArea` answers from the
        nearest gridpoint among everything the process holds, so the filter is
        the only thing that keeps one organisation's coordinates out of
        another's grid."""
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)) as upstream:
            self.forecast()

        self.assertEqual(
            upstream.call_args.kwargs["params"],
            {"lat": -1.2864, "lon": 36.8172, "area": AREA},
        )

    def test_the_upstream_is_one_fixed_service_for_the_instance(self):
        """D14 replaced a pair per organisation with one pair, so there is no
        slug in the name and no registry that could fall out of step with it."""
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)) as upstream:
            self.forecast()

        self.assertEqual(upstream.call_args[0][0], "http://forti-jsonfrontend:8080/")

    def test_gzip_is_not_requested_from_the_upstream(self):
        """Asking for it would only mean decompressing a body we re-serve."""
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)) as upstream:
            self.forecast()

        self.assertEqual(upstream.call_args.kwargs["headers"]["Accept-Encoding"], "identity")

    def test_outside_coverage_is_passed_through_as_the_200_it_is(self):
        """For a *loaded* area, "too far from any gridpoint" arrives as a 200
        carrying an error body, and the view must not reinterpret it."""
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(COVERAGE_ERROR)):
            response = self.forecast()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), COVERAGE_ERROR)


class TheAreaCheckTests(ServingTestCase):
    """`meta.area` is checked for **equality** on every response (D16)."""

    def setUp(self):
        super().setUp()
        served_model()

    def test_an_area_can_never_be_omitted_from_an_upstream_request(self):
        """The sharp edge, made unreachable rather than unlikely: an omitted
        area is not "no areas", it is every organisation's areas."""
        with self.assertRaises(RuntimeError) as ctx:
            upstream_params("", -1.2864, 36.8172)

        self.assertIn("every organisation", str(ctx.exception))

    def test_another_organisation_s_area_answering_is_a_502(self):
        answered = {
            **DOCUMENT,
            "properties": {**DOCUMENT["properties"], "meta": {"area": "ke-kmd.ecmwf-ifs"}},
        }

        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(answered)):
            with self.assertLogs("georiva_publisher_forti.serving", level="ERROR") as logs:
                response = self.forecast()

        self.assertEqual(response.status_code, 502)
        self.assertNotIn("timeseries", json.dumps(response.json()))
        self.assertIn("ke-kmd.ecmwf-ifs", logs.output[0])

    def test_a_forecast_naming_no_area_at_all_is_a_502(self):
        """A document with data has to say who produced it. Only the coverage
        error, which carries none, may be silent about its area."""
        anonymous = {
            **DOCUMENT,
            "properties": {**DOCUMENT["properties"], "meta": {"updated_at": "2026-09-02T12:00:00Z"}},
        }

        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(anonymous)):
            with self.assertLogs("georiva_publisher_forti.serving", level="ERROR"):
                response = self.forecast()

        self.assertEqual(response.status_code, 502)

    def test_the_coverage_error_body_is_allowed_to_name_no_area(self):
        """`EncodeError` (`encode.go:30`) never sets `Area`, so requiring one
        here would turn every out-of-range point into a 502."""
        self.assertIsNone(area_objection(COVERAGE_ERROR, AREA))

    def test_a_body_that_is_not_a_document_is_refused(self):
        self.assertIsNotNone(area_objection(["not", "a", "document"], AREA))


class UpstreamFailureTests(ServingTestCase):
    def setUp(self):
        super().setUp()
        served_model()

    def test_an_unreachable_pair_is_a_503_not_a_500(self):
        with patch(
            "georiva_publisher_forti.serving.requests.get",
            side_effect=requests.ConnectionError("no such host"),
        ):
            response = self.forecast()

        self.assertEqual(response.status_code, 503)

    def test_an_upstream_failure_is_not_reported_as_the_caller_s_fault(self):
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(status_code=500)):
            response = self.forecast()

        self.assertEqual(response.status_code, 503)

    def test_a_model_geo_riva_serves_but_the_reader_has_not_loaded_is_a_503(self):
        """The upstream says 404 "Outside of coverage area", and with no
        `geographic_extent` written that can only mean the named area is not
        resident. Passing it through would blame the caller's coordinates."""
        upstream = FakeResponse(status_code=404, text="Outside of coverage area\n")

        with patch("georiva_publisher_forti.serving.requests.get", return_value=upstream):
            with self.assertLogs("georiva_publisher_forti.serving", level="ERROR") as logs:
                response = self.forecast()

        self.assertEqual(response.status_code, 503)
        self.assertIn(AREA, logs.output[0])

    def test_a_request_the_reader_refuses_is_our_fault_not_the_caller_s(self):
        """`http.Error` writes prose, which does not parse — and every parameter
        the reader saw was ours, so a 4xx is a 502 rather than a passthrough."""
        upstream = FakeResponse(status_code=400, text="Empty value for area\n")

        with patch("georiva_publisher_forti.serving.requests.get", return_value=upstream):
            with self.assertLogs("georiva_publisher_forti.serving", level="ERROR") as logs:
                response = self.forecast()

        self.assertEqual(response.status_code, 502)
        self.assertIn("Empty value for area", logs.output[0])

    def test_a_200_that_does_not_parse_is_a_502(self):
        with patch(
            "georiva_publisher_forti.serving.requests.get",
            return_value=FakeResponse(status_code=200, text="not json at all"),
        ):
            with self.assertLogs("georiva_publisher_forti.serving", level="ERROR"):
                response = self.forecast()

        self.assertEqual(response.status_code, 502)


class TheRequestTests(ServingTestCase):
    def setUp(self):
        super().setUp()
        served_model()

    def test_a_missing_coordinate_never_reaches_the_upstream(self):
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            response = self.client.get("/api/forecast/ecmwf-ifs/", {"lon": 36.8})

        self.assertEqual(response.status_code, 400)
        upstream.assert_not_called()

    def test_an_out_of_range_coordinate_never_reaches_the_upstream(self):
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            response = self.forecast(lat=91, lon=36.8)

        self.assertEqual(response.status_code, 400)
        upstream.assert_not_called()

    def test_a_caller_s_own_area_is_refused_rather_than_ignored(self):
        """Dropping it silently would leave a caller believing a filter applied,
        and the filter it was asking for is the tenant boundary."""
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            response = self.forecast(lat=-1.2864, lon=36.8172, area="ke-kmd.ecmwf-ifs")

        self.assertEqual(response.status_code, 400)
        self.assertIn("area", response.json()["detail"])
        upstream.assert_not_called()

    def test_a_caller_s_topography_is_refused(self):
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            response = self.forecast(lat=-1.2864, lon=36.8172, topography="anything")

        self.assertEqual(response.status_code, 400)
        upstream.assert_not_called()

    def test_an_unreadable_model_404s_before_the_query_is_judged(self):
        """Otherwise the shape of the error tells a caller whether a model they
        may not see exists."""
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            response = self.client.get("/api/forecast/no-such-model/", {"area": "x"})

        self.assertEqual(response.status_code, 404)
        upstream.assert_not_called()


class VisibilityTests(ServingTestCase):
    """D18: absent from the listing *and* 404 on the URL, by one query."""

    def assertUnservable(self, model="ecmwf-ifs"):
        with patch("georiva_publisher_forti.serving.requests.get") as upstream:
            detail = self.forecast(model)
            listing = self.listing()

        self.assertEqual(detail.status_code, 404)
        self.assertEqual([entry["model"] for entry in listing.json()["models"]], [])
        upstream.assert_not_called()

    def assertServable(self, model="ecmwf-ifs"):
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
            detail = self.forecast(model)
            listing = self.listing()

        self.assertEqual(detail.status_code, 200)
        self.assertEqual([entry["model"] for entry in listing.json()["models"]], [model])

    def test_a_public_model_is_served_to_anybody(self):
        served_model()

        self.assertServable()

    def test_a_private_model_is_invisible_to_a_stranger(self):
        served_model(collection_visibility=Collection.Visibility.PRIVATE)

        self.assertUnservable()

    def test_a_private_model_is_served_to_a_member(self):
        served_model(collection_visibility=Collection.Visibility.PRIVATE)
        user = get_user_model().objects.create_user(
            username="member", email="member@example.test", password="forti-serving-test-pw"
        )
        join_org(user, self.org_slug)
        self.client.force_login(user)

        self.assertServable()

    def test_a_model_narrowed_below_its_public_collection_is_invisible(self):
        """The field exists for exactly this: an NMHS serving maps publicly and
        point forecasts to members only (D18)."""
        served_model(visibility=FortiPublication.Visibility.PRIVATE)

        self.assertUnservable()

    def test_narrowing_the_collection_afterwards_takes_the_model_with_it(self):
        """`clean()` refuses a publication wider than its collection, but it runs
        when the *publication* is saved. Narrowing the collection is an ordinary
        edit by somebody with no reason to know a Forti publication exists."""
        publication = served_model()
        Collection.objects.filter(pk=publication.collection_id).update(visibility=Collection.Visibility.PRIVATE)

        self.assertUnservable()

    def test_a_model_that_has_never_published_is_invisible(self):
        """`rawdataforecaster` has never been told to load it, so advertising it
        would produce a 404 blaming the caller's coordinates."""
        make_publication(make_collection(), slug="ecmwf-ifs")

        self.assertUnservable()

    def test_a_disabled_model_is_invisible(self):
        publication = served_model()
        FortiPublication.objects.filter(pk=publication.pk).update(is_enabled=False)

        self.assertUnservable()

    def test_a_retired_collection_takes_its_model_off_the_air(self):
        publication = served_model()
        Collection.objects.filter(pk=publication.collection_id).update(is_active=False)

        self.assertUnservable()

    def test_another_organisation_s_model_is_not_reachable_on_this_host(self):
        """The organisation comes from the host, never the session."""
        served_model(org_slug="ke-kmd")
        make_organisation(self.org_slug)

        self.assertUnservable()

    def test_the_same_model_is_served_on_its_own_organisation_s_host(self):
        served_model(org_slug="ke-kmd")
        self.client.defaults["HTTP_HOST"] = org_host("ke-kmd")

        answered = {
            **DOCUMENT,
            "properties": {**DOCUMENT["properties"], "meta": {"area": "ke-kmd.ecmwf-ifs"}},
        }
        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(answered)) as upstream:
            response = self.forecast()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.call_args.kwargs["params"]["area"], "ke-kmd.ecmwf-ifs")


class ServableSetTests(TestCase):
    """What the route serves must never be more than what the reader was told
    to load, or the difference is a 503 nobody predicted."""

    def test_the_served_set_is_a_subset_of_the_configured_area_list(self):
        published = served_model()
        never_published = make_publication(make_collection(slug="ifs-pressure"), slug="ecmwf-ifs-pressure")
        retired = publish(make_publication(make_collection(slug="ifs-old"), slug="ecmwf-ifs-old"))
        Collection.objects.filter(pk=retired.collection_id).update(is_active=False)

        served = set(FortiPublication.objects.servable().values_list("pk", flat=True))
        configured = {publication.pk for publication in rdfconfig.servable(list(FortiPublication.objects.all()))}

        self.assertEqual(served, {published.pk})
        self.assertNotIn(never_published.pk, configured)
        self.assertLessEqual(served, configured)


class CacheMarkingTests(ServingTestCase):
    """The `/api/` cache key is `$host$request_uri` and carries no identity
    (ADR 0029), so `public` means "hand this to whoever asks next"."""

    def test_a_public_model_s_forecast_may_be_shared(self):
        served_model()

        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
            response = self.forecast()

        self.assertEqual(response["Cache-Control"], "public, max-age=1800")

    def test_the_max_age_is_the_plugin_s_and_an_operator_may_change_it(self):
        served_model()

        with override_settings(GEORIVA_FORTI_FORECAST_MAX_AGE=60):
            with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
                response = self.forecast()

        self.assertEqual(response["Cache-Control"], "public, max-age=60")

    def test_a_private_model_s_forecast_is_never_shared(self):
        served_model(collection_visibility=Collection.Visibility.PRIVATE)
        user = get_user_model().objects.create_user(
            username="member", email="member@example.test", password="forti-serving-test-pw"
        )
        join_org(user, self.org_slug)
        self.client.force_login(user)

        with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
            response = self.forecast()

        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_an_error_is_never_cacheable(self):
        served_model()

        with patch(
            "georiva_publisher_forti.serving.requests.get",
            side_effect=requests.ConnectionError("no such host"),
        ):
            response = self.forecast()

        self.assertEqual(response["Cache-Control"], "no-store")

    def test_the_listing_is_never_cacheable(self):
        """It varies by audience at a URL that carries none. nginx's
        `proxy_no_cache` on the session cookie would cover it, but resting on
        that is the enumeration ADR 0029 refuses to depend on."""
        served_model()

        self.assertEqual(self.listing()["Cache-Control"], "no-store")


class ListingTests(ServingTestCase):
    def test_what_a_consumer_is_told_about_a_model(self):
        served_model()

        entry = self.listing().json()["models"][0]

        self.assertEqual(entry["model"], "ecmwf-ifs")
        self.assertEqual(entry["title"], "ECMWF IFS")
        self.assertEqual(entry["visibility"], "public")
        self.assertEqual(entry["bbox"], [32.0, 4.0, 33.25, 5.25])
        self.assertTrue(entry["reference_time"].startswith("2026-09-02T12:00:00"))

    def test_parameters_are_named_in_the_vocabulary_the_document_uses(self):
        """`published_parameters` holds forti's internal names, which appear
        nowhere a consumer reads."""
        served_model()

        entry = self.listing().json()["models"][0]

        self.assertEqual(entry["parameters"], ["air_temperature", "symbol_code"])

    def test_a_parameter_this_version_does_not_know_is_dropped_not_guessed(self):
        publish(served_model(), parameters=("air_temperature_2m", "invented_by_a_later_version"))

        entry = self.listing().json()["models"][0]

        self.assertEqual(entry["parameters"], ["air_temperature"])

    def test_nothing_operational_is_published(self):
        """Versions, grid ids and build state answer "is the pipeline healthy",
        which is the verification panel's question and not a consumer's."""
        served_model()

        entry = self.listing().json()["models"][0]

        self.assertEqual(
            set(entry),
            {"model", "title", "visibility", "bbox", "reference_time", "parameters"},
        )


class ThrottleTests(ServingTestCase):
    """The instance had no throttling before this route, and core still has no
    `DEFAULT_THROTTLE_RATES` — so the rate is the plugin's."""

    def test_a_throttled_request_is_a_429_and_not_a_500(self):
        """`ScopedRateThrottle` would have raised `ImproperlyConfigured` here:
        its rate comes from a settings key core does not have and D19 forbids
        adding."""
        served_model()

        with override_settings(GEORIVA_FORTI_FORECAST_THROTTLE_RATE="1/min"):
            with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
                first = self.forecast()
                second = self.forecast()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_the_throttle_can_be_turned_off_by_an_operator(self):
        served_model()

        with override_settings(GEORIVA_FORTI_FORECAST_THROTTLE_RATE=None):
            with patch("georiva_publisher_forti.serving.requests.get", return_value=FakeResponse(DOCUMENT)):
                for _ in range(3):
                    response = self.forecast()

        self.assertEqual(response.status_code, 200)

    def test_one_organisation_s_traffic_does_not_spend_another_s_allowance(self):
        """Anonymous callers are keyed on IP, so two tenants behind one NAT would
        otherwise share a bucket."""
        served_model()
        served_model(org_slug="ke-kmd")

        with override_settings(GEORIVA_FORTI_FORECAST_THROTTLE_RATE="1/min"):
            with patch(
                "georiva_publisher_forti.serving.requests.get",
                return_value=FakeResponse({**DOCUMENT, "properties": {**DOCUMENT["properties"]}}),
            ):
                self.forecast()
                spent = self.forecast()

                self.client.defaults["HTTP_HOST"] = org_host("ke-kmd")
                other = self.forecast()

        self.assertEqual(spent.status_code, 429)
        self.assertNotEqual(other.status_code, 429)
