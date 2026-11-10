"""``GET /api/forecast/{model}/places/`` — a model's forecast told for its towns.

Tested through the HTTP client with jsonfrontend stubbed at its own boundary
(``requests.get``), answering per coordinate the way the real reader does, and
the reader's status document stubbed at :func:`verification.resident_areas`.

What the route promises:

- the towns are the instance-wide gazetteer plus the **owning** organisation's
  additions, inside the publication's bbox, whoever asks;
- a town the reader has no forecast for is dropped, not shown empty;
- the default window is the finest the model publishes; ``window=`` picks
  another of the ones it has;
- with no ``time`` the whole run comes back as arrays, with ``time`` one
  GeoJSON slice; both read one matrix, built once per published version;
- a build is refused while the reader still holds an older version, and thrown
  away if the reader reloads under it.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from georiva.core.models import Town
from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG, dial_org, make_organisation
from georiva_publisher_forti.verification import ResidentAreas

from .factories import REFERENCE_TIME, make_collection, make_publication
from .test_serving import FakeResponse, publish

AREA = f"{DEFAULT_TEST_ORG_SLUG}.ecmwf-ifs"
VERSION = 178895520014
LOADED_AT = "2026-09-02T14:00:00Z"

# Inside BBOX (32.0, 4.0, 33.25, 5.25).
JUBA = {"name": "Juba", "latitude": 4.85, "longitude": 31.6}  # west of the box
TOWN_A = {"name": "Alpha", "latitude": 4.5, "longitude": 32.5, "feature_code": "PPLA", "population": 50000}
TOWN_B = {"name": "Bravo", "latitude": 5.0, "longitude": 33.0, "feature_code": "PPL", "population": 9000}


def step(time, *, temperature, symbol6=None, precip6=None, symbol1=None, precip1=None, wind=(3.0, 90.0)):
    data = {
        "instant": {
            "details": {
                "air_temperature": temperature,
                "wind_speed": wind[0],
                "wind_from_direction": wind[1],
                "relative_humidity": 80.0,
            }
        }
    }
    if symbol6 is not None:
        data["next_6_hours"] = {
            "summary": {"symbol_code": symbol6},
            "details": {"precipitation_amount": precip6, "air_temperature_max": 30.0},
        }
    if symbol1 is not None:
        data["next_1_hours"] = {"summary": {"symbol_code": symbol1}, "details": {"precipitation_amount": precip1}}
    return {"time": time, "data": data}


def document(timeseries, *, area=AREA, updated_at=LOADED_AT):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {
            "meta": {
                "updated_at": updated_at,
                "units": {"air_temperature": "celsius", "wind_speed": "m/s", "precipitation_amount": "mm"},
                "area": area,
            },
            "timeseries": timeseries,
        },
    }


#: What the reader returns for a point too far from any gridpoint: no area, no series.
COVERAGE_ERROR = {
    "type": "Feature",
    "geometry": {"type": "Point", "coordinates": [0, 0]},
    "properties": {
        "meta": {"updated_at": LOADED_AT, "error": "no data at the given location", "units": {}},
        "timeseries": [],
    },
}

#: A 3-hourly model: 6h windows, no 1h.
IFS_A = document(
    [
        step("2026-09-02T15:00:00Z", temperature=24.1, symbol6="rainshowers_day", precip6=1.2, wind=(4.0, 95.0)),
        step("2026-09-02T18:00:00Z", temperature=21.0, symbol6="partlycloudy_night", precip6=0.0),
        step("2026-09-02T21:00:00Z", temperature=19.5),  # past the last full window
    ]
)
IFS_B = document(
    [
        step("2026-09-02T15:00:00Z", temperature=27.3, symbol6="fair_day", precip6=0.0),
        step("2026-09-02T18:00:00Z", temperature=23.2, symbol6="clearsky_night", precip6=0.0),
        step("2026-09-02T21:00:00Z", temperature=22.0),
    ]
)

#: An hourly model: 1h and 6h windows.
WRF_A = document(
    [
        step(
            "2026-09-02T15:00:00Z",
            temperature=24.0,
            symbol1="rain_day",
            precip1=0.8,
            symbol6="rainshowers_day",
            precip6=2.0,
        ),
        step("2026-09-02T16:00:00Z", temperature=23.0, symbol1="cloudy", precip1=0.0),
    ]
)


class Reader:
    """jsonfrontend, answering each town by its coordinate."""

    def __init__(self, by_point):
        self.by_point = {(round(lat, 4), round(lon, 4)): doc for (lat, lon), doc in by_point.items()}
        self.calls = 0

    def __call__(self, url, params=None, **kwargs):
        self.calls += 1
        doc = self.by_point.get((round(params["lat"], 4), round(params["lon"], 4)), COVERAGE_ERROR)
        if isinstance(doc, FakeResponse):
            return doc
        return FakeResponse(doc)


def resident(loaded):
    return ResidentAreas("present", by_area={AREA: (loaded, loaded)})


class PlacesTestCase(TestCase):
    def setUp(self):
        override = override_settings(
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": self.id()}}
        )
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        dial_org(self.client, DEFAULT_TEST_ORG_SLUG)

        collection = make_collection()
        self.publication = publish(make_publication(collection, slug="ecmwf-ifs"))
        self.owner = self.publication.organisation
        self.a = Town.objects.create(country_code="SS", **TOWN_A)
        self.b = Town.objects.create(country_code="SS", **TOWN_B)

        residency = patch("georiva_publisher_forti.places.resident_areas", side_effect=lambda **kw: resident(VERSION))
        self.residency = residency.start()
        self.addCleanup(residency.stop)

    def reader(self, a=IFS_A, b=IFS_B, extra=None):
        points = {(TOWN_A["latitude"], TOWN_A["longitude"]): a, (TOWN_B["latitude"], TOWN_B["longitude"]): b}
        points.update(extra or {})
        return Reader(points)

    def places(self, reader, **params):
        with patch("georiva_publisher_forti.serving.requests.get", side_effect=reader):
            return self.client.get("/api/forecast/ecmwf-ifs/places/", params)


class WholeRunTests(PlacesTestCase):
    def test_the_whole_run_comes_back_as_arrays_per_town(self):
        response = self.places(self.reader())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "ecmwf-ifs")
        self.assertEqual(body["reference_time"], "2026-09-02T12:00:00Z")
        self.assertEqual(body["window"], {"duration": "PT6H"})
        self.assertEqual(body["times"], ["2026-09-02T15:00:00Z", "2026-09-02T18:00:00Z"])
        self.assertEqual([t["name"] for t in body["towns"]], ["Alpha", "Bravo"])
        self.assertEqual(body["towns"][0]["coordinates"], [32.5, 4.5])
        self.assertEqual(body["towns"][0]["rank"], Town.Rank.ADMIN1_SEAT)
        values = body["values"]
        self.assertEqual(
            values["symbol_code"], [["rainshowers_day", "partlycloudy_night"], ["fair_day", "clearsky_night"]]
        )
        self.assertEqual(values["air_temperature"], [[24.1, 21.0], [27.3, 23.2]])
        self.assertEqual(values["wind_speed"][0], [4.0, 3.0])
        self.assertEqual(values["wind_from_direction"][0], [95.0, 90.0])
        self.assertEqual(values["precipitation_amount"], [[1.2, 0.0], [0.0, 0.0]])
        self.assertEqual(body["units"]["air_temperature"], "celsius")

    def test_the_matrix_is_built_once_per_published_version(self):
        reader = self.reader()
        self.places(reader)
        self.places(reader)
        self.places(reader, time="2026-09-02T15:00:00Z")

        self.assertEqual(reader.calls, 2)

    def test_a_new_version_builds_a_new_matrix(self):
        reader = self.reader()
        self.places(reader)
        type(self.publication).objects.filter(pk=self.publication.pk).update(published_version=VERSION + 1)
        self.residency.side_effect = lambda **kw: resident(VERSION + 1)
        self.places(reader)

        self.assertEqual(reader.calls, 4)

    def test_a_public_model_s_places_may_be_shared_and_carry_an_etag(self):
        response = self.places(self.reader())

        self.assertTrue(response["Cache-Control"].startswith("public"))
        self.assertIn(str(VERSION), response["ETag"])

    def test_an_unchanged_matrix_answers_304(self):
        reader = self.reader()
        etag = self.places(reader)["ETag"]
        with patch("georiva_publisher_forti.serving.requests.get", side_effect=reader):
            response = self.client.get("/api/forecast/ecmwf-ifs/places/", HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(response.status_code, 304)


class TownSelectionTests(PlacesTestCase):
    def test_a_town_outside_the_bbox_is_not_asked_for(self):
        Town.objects.create(country_code="SS", **JUBA)
        body = self.places(self.reader()).json()

        self.assertNotIn("Juba", [t["name"] for t in body["towns"]])

    def test_an_inactive_town_is_left_out(self):
        Town.objects.filter(pk=self.b.pk).update(is_active=False)
        body = self.places(self.reader()).json()

        self.assertEqual([t["name"] for t in body["towns"]], ["Alpha"])

    def test_the_owner_s_own_towns_are_included(self):
        own = {"name": "Charlie", "latitude": 4.2, "longitude": 32.2}
        Town.objects.create(organisation=self.owner, country_code="SS", **own)
        reader = self.reader(extra={(4.2, 32.2): IFS_B})
        body = self.places(reader).json()

        self.assertIn("Charlie", [t["name"] for t in body["towns"]])

    def test_another_organisation_s_towns_are_not(self):
        other = make_organisation("other-org")
        Town.objects.create(organisation=other, country_code="SS", name="Delta", latitude=4.2, longitude=32.2)
        reader = self.reader(extra={(4.2, 32.2): IFS_B})
        body = self.places(reader).json()

        self.assertNotIn("Delta", [t["name"] for t in body["towns"]])

    def test_a_town_the_reader_has_no_forecast_for_is_dropped(self):
        body = self.places(self.reader(b=COVERAGE_ERROR)).json()

        self.assertEqual([t["name"] for t in body["towns"]], ["Alpha"])

    def test_towns_come_most_prominent_first(self):
        Town.objects.create(country_code="SS", name="Capital", latitude=4.9, longitude=32.9, feature_code="PPLC")
        reader = self.reader(extra={(4.9, 32.9): IFS_B})
        body = self.places(reader).json()

        self.assertEqual([t["name"] for t in body["towns"]], ["Capital", "Alpha", "Bravo"])


class WindowTests(PlacesTestCase):
    def test_an_hourly_model_defaults_to_its_hourly_window(self):
        body = self.places(self.reader(a=WRF_A, b=COVERAGE_ERROR)).json()

        self.assertEqual(body["window"], {"duration": "PT1H"})
        self.assertEqual(body["times"], ["2026-09-02T15:00:00Z", "2026-09-02T16:00:00Z"])
        self.assertEqual(body["values"]["symbol_code"], [["rain_day", "cloudy"]])
        self.assertEqual(body["values"]["precipitation_amount"], [[0.8, 0.0]])

    def test_a_coarser_window_can_be_asked_for(self):
        body = self.places(self.reader(a=WRF_A, b=COVERAGE_ERROR), window="PT6H").json()

        self.assertEqual(body["window"], {"duration": "PT6H"})
        self.assertEqual(body["times"], ["2026-09-02T15:00:00Z"])
        self.assertEqual(body["values"]["precipitation_amount"], [[2.0]])

    def test_a_window_the_model_does_not_publish_is_a_400_naming_the_ones_it_does(self):
        response = self.places(self.reader(), window="PT1H")

        self.assertEqual(response.status_code, 400)
        self.assertIn("PT6H", response.json()["detail"])


class SliceTests(PlacesTestCase):
    def test_one_time_comes_back_as_geojson(self):
        response = self.places(self.reader(), time="2026-09-02T18:00:00Z")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "FeatureCollection")
        self.assertEqual(body["time"], "2026-09-02T18:00:00Z")
        self.assertEqual(
            body["window"],
            {"duration": "PT6H", "start": "2026-09-02T18:00:00Z", "end": "2026-09-03T00:00:00Z"},
        )
        self.assertEqual(body["times"], ["2026-09-02T15:00:00Z", "2026-09-02T18:00:00Z"])
        first = body["features"][0]
        self.assertEqual(first["geometry"], {"type": "Point", "coordinates": [32.5, 4.5]})
        self.assertEqual(
            first["properties"],
            {
                "id": self.a.pk,
                "name": "Alpha",
                "rank": Town.Rank.ADMIN1_SEAT,
                "population": 50000,
                "symbol_code": "partlycloudy_night",
                "air_temperature": 21.0,
                "wind_speed": 3.0,
                "wind_from_direction": 90.0,
                "precipitation_amount": 0.0,
            },
        )

    def test_a_time_may_be_written_with_an_offset(self):
        body = self.places(self.reader(), time="2026-09-02T18:00:00+00:00").json()

        self.assertEqual(body["time"], "2026-09-02T18:00:00Z")

    def test_a_time_the_window_cannot_answer_is_a_404(self):
        response = self.places(self.reader(), time="2026-09-02T21:00:00Z")

        self.assertEqual(response.status_code, 404)

    def test_an_unreadable_time_is_a_400(self):
        response = self.places(self.reader(), time="tomorrow")

        self.assertEqual(response.status_code, 400)


class BuildSafetyTests(PlacesTestCase):
    def test_while_the_reader_holds_an_older_version_nothing_is_built(self):
        self.residency.side_effect = lambda **kw: resident(VERSION - 1)
        reader = self.reader()
        response = self.places(reader)

        self.assertEqual(response.status_code, 503)
        self.assertIn("Retry-After", response)
        self.assertEqual(reader.calls, 0)

    def test_a_reload_under_the_build_is_thrown_away(self):
        reloaded = document(IFS_B["properties"]["timeseries"], updated_at="2026-09-02T14:05:00Z")
        response = self.places(self.reader(b=reloaded))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.places(self.reader()).status_code, 200)

    def test_another_area_answering_is_a_502(self):
        foreign = document(IFS_B["properties"]["timeseries"], area="other.model")
        response = self.places(self.reader(b=foreign))

        self.assertEqual(response.status_code, 502)

    def test_an_upstream_failure_is_a_503_and_is_not_cached(self):
        response = self.places(self.reader(b=FakeResponse(status_code=500, text="boom")))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.places(self.reader()).status_code, 200)

    def test_a_build_in_progress_elsewhere_is_waited_on_not_repeated(self):
        from georiva_publisher_forti import places

        cache.add(places.lock_key(self.publication), 1, timeout=60)
        with override_settings(GEORIVA_FORTI_PLACES_WAIT=0):
            reader = self.reader()
            response = self.places(reader)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(reader.calls, 0)


class VisibilityTests(PlacesTestCase):
    def test_a_model_the_caller_may_not_see_is_a_404(self):
        response = self.client.get("/api/forecast/nope/places/")

        self.assertEqual(response.status_code, 404)

    def test_a_model_that_has_never_published_is_a_404(self):
        type(self.publication).objects.filter(pk=self.publication.pk).update(published_version=None)

        self.assertEqual(self.places(self.reader()).status_code, 404)


# The fixture's reference time, spelled the way the response spells it.
assert REFERENCE_TIME.isoformat() == "2026-09-02T12:00:00+00:00"
