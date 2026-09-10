"""What the area list may say, and what it must never say."""

from django.test import TestCase

from georiva_publisher_forti import rdfconfig

from .factories import make_collection, make_publication


class ServableTests(TestCase):
    """The rule that keeps the instance's whole point-forecast API from crash-looping."""

    def setUp(self):
        self.collection = make_collection()

    def test_a_published_area_is_servable(self):
        publication = make_publication(self.collection, published_version=178835040000)

        self.assertEqual(rdfconfig.servable([publication]), [publication])

    def test_an_enabled_area_that_has_never_published_is_not(self):
        """Enabled is a statement of intent; published is a statement of fact.

        A configured area with no ``latest/<area key>`` used to read as version 0,
        so the reader looked for ``<area key>/0/complete.json``, did not find it,
        and exited at startup. M5.1 fixed that in the fork; this stays as the belt
        to that fix's braces, and as the half that holds against an image nobody
        has rebuilt.
        """
        publication = make_publication(self.collection, published_version=None)

        self.assertTrue(publication.is_enabled)
        self.assertEqual(rdfconfig.servable([publication]), [])

    def test_a_disabled_area_is_not_servable_even_once_published(self):
        publication = make_publication(self.collection, published_version=178835040000, is_enabled=False)

        self.assertEqual(rdfconfig.servable([publication]), [])


class MaxSizeGibTests(TestCase):
    def setUp(self):
        self.collection = make_collection()

    def test_a_small_area_gets_the_floor_not_zero(self):
        """A real area is megabytes; a ceiling of 0 GiB would refuse to load it."""
        publication = make_publication(self.collection, point_count=1920, published_step_count=15)

        self.assertEqual(rdfconfig.max_size_gib(publication), 1)

    def test_a_large_area_rounds_up(self):
        publication = make_publication(self.collection, point_count=20_000_000, published_step_count=68)

        # 20e6 * 68 * 2 bytes is ~2.53 GiB, and a ceiling below that would refuse
        # the very area it was computed for.
        self.assertEqual(rdfconfig.max_size_gib(publication), 3)

    def test_an_unpublished_area_does_not_divide_by_a_zero_step_count(self):
        publication = make_publication(self.collection, point_count=1920, published_step_count=0)

        self.assertEqual(rdfconfig.max_size_gib(publication), 1)


class BuildTests(TestCase):
    def setUp(self):
        self.collection = make_collection()
        self.publication = make_publication(
            self.collection,
            slug="ecmwf-ifs",
            published_version=178835040000,
            point_count=1920,
            published_step_count=15,
        )

    def config(self, publications=None):
        return rdfconfig.build(
            publications if publications is not None else [self.publication],
            bucket="georiva-publications",
            endpoint="http://georiva-minio:9000",
            prefix="_forti/",
        )

    def test_an_area_is_named_by_its_key_not_its_slug(self):
        """``{org}.{slug}``. The slug alone is only unique within an organisation,
        and one process now holds every organisation's areas."""
        self.assertEqual(self.config()["areas"], [self.publication.area_key])
        self.assertNotEqual(self.publication.area_key, self.publication.slug)

    def test_max_size_gib_is_an_object_keyed_by_the_same_key(self):
        """``dataset.go:50`` asserts a map. The documented bare number panics — and
        a limit naming an area not in ``areas`` is a Problem the reader refuses
        (`config/reload.go`), so the two have to be keyed identically."""
        configuration = self.config()["loader"]["configuration"]

        self.assertEqual(configuration["max_size_gib"], {self.publication.area_key: 1})

    def test_the_prefix_bounds_the_reader_to_forti_and_to_nothing_else(self):
        """D3 said the prefix was the whole of the tenant isolation. D14/D15
        superseded that: there is one prefix for the instance, so ``Latest()``
        parses every organisation's markers by design."""
        bucket = self.config()["source"]["bucket"]

        self.assertIn("prefix=_forti/", bucket)
        self.assertTrue(bucket.startswith("s3://georiva-publications?"))

    def test_an_https_endpoint_is_not_marked_insecure(self):
        config = rdfconfig.build(
            [self.publication],
            bucket="georiva-publications",
            endpoint="https://s3.example.org",
            prefix="_forti/",
        )

        self.assertIn("disable_https=false", config["source"]["bucket"])
        self.assertIn("endpoint=https://s3.example.org", config["source"]["bucket"])

    def test_an_unpublished_area_reaches_neither_the_list_nor_the_ceiling(self):
        unpublished = make_publication(
            make_collection(slug="aifs-surface"),
            slug="aifs",
            published_version=None,
        )

        config = self.config([self.publication, unpublished])

        self.assertEqual(config["areas"], [self.publication.area_key])
        self.assertNotIn(unpublished.area_key, config["loader"]["configuration"]["max_size_gib"])

    def test_the_list_spans_every_organisation_on_the_instance(self):
        """One file governs every tenant's serving, so a second organisation's
        area has to appear in it — and appear beside the first, not instead."""
        other = make_publication(
            make_collection(org_slug="other-org"),
            slug="gfs",
            published_version=178835040000,
            point_count=1920,
            published_step_count=15,
        )

        config = self.config([self.publication, other])

        self.assertNotEqual(self.publication.organisation, other.organisation)
        self.assertEqual(config["areas"], sorted([self.publication.area_key, other.area_key]))

    def test_the_distance_is_carried_through(self):
        config = rdfconfig.build(
            [self.publication],
            bucket="b",
            endpoint="http://e:9000",
            prefix="_forti/",
            maximum_gridpoint_distance=25000,
        )

        self.assertEqual(config["maximum_gridpoint_distance"], 25000)

    def test_the_default_distance_is_the_measured_one(self):
        """§10: 50 km, against the ≈19.7 km interior worst case."""
        self.assertEqual(rdfconfig.DEFAULT_MAXIMUM_GRIDPOINT_DISTANCE, 50000)
        self.assertEqual(self.config()["maximum_gridpoint_distance"], 50000)
