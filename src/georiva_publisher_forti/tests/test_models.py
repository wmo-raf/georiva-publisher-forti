"""What a publication refuses to be.

Two of these are the whole of D12 and the area half of D3, and both would fail
silently rather than loudly. A private collection published to Forti is served to
anyone who can reach the reader, because the reader presents no credential and
there is nobody to ask. Two areas sharing a name means one of them is
unreachable — and a *missing* ``latest/<area>`` reads as version **0**, so
``rawdataforecaster`` goes looking for ``<area>/0/complete.json``, which is fatal
at startup and merely logged afterwards, in an error that does not mention the
area name at all (`forecast.go:143`).
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from georiva.core.models import Collection
from georiva.core.publishing import PublicationSink
from georiva_publisher_forti.models import MARKER_PATTERNS, SINK_ROOT, FortiPublication

from .factories import make_collection, make_publication


class ValidationTests(TestCase):
    def test_a_public_collection_is_publishable(self):
        publication = make_publication(make_collection())

        publication.full_clean()

    def test_a_private_collection_is_refused(self):
        collection = make_collection(visibility=Collection.Visibility.PRIVATE)
        publication = make_publication(collection)

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("collection", ctx.exception.message_dict)

    def test_an_internal_collection_is_refused(self):
        collection = make_collection(visibility=Collection.Visibility.INTERNAL)
        publication = make_publication(collection)

        with self.assertRaises(ValidationError):
            publication.full_clean()

    def test_two_areas_of_one_organisation_may_not_share_a_name(self):
        first = make_publication(make_collection(slug="global"), area="kenya")
        second = FortiPublication(
            collection=make_collection(slug="national"),
            area="kenya",
            west=1,
            south=1,
            east=2,
            north=2,
        )
        second.collection.catalog.organisation = first.collection.catalog.organisation
        second.collection.catalog.save()

        with self.assertRaises(ValidationError) as ctx:
            second.full_clean()

        self.assertIn("area", ctx.exception.message_dict)

    def test_a_backwards_extent_is_refused(self):
        publication = make_publication(make_collection(), bbox=(42.0, -5.0, 33.0, 6.0))

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("east", ctx.exception.message_dict)


class SinkTests(TestCase):
    def test_two_organisations_publish_into_one_prefix(self):
        """One rawdataforecaster for the instance, given one prefix, listing
        every organisation's areas under it (D14/D15)."""
        first = make_publication(make_collection(slug="global"), area="global")
        second = make_publication(make_collection(org_slug="other-org"), area="national")

        self.assertNotEqual(first.organisation, second.organisation)
        self.assertEqual(first.sink().root, second.sink().root)
        self.assertEqual(first.sink().root, f"{SINK_ROOT}/")

    def test_the_prefix_is_not_a_name_any_organisation_could_hold(self):
        """The prefix is no longer the tenancy boundary, so what is left to
        guarantee is that it cannot become some organisation's own."""
        publication = make_publication(make_collection())

        self.assertTrue(publication.sink().is_instance_wide)
        with self.assertRaises(ValueError):
            PublicationSink.instance_wide(SINK_ROOT.lstrip("_"))

    def test_the_sink_knows_which_paths_are_markers(self):
        publication = make_publication(make_collection())
        sink = publication.sink()
        key = publication.area_key

        self.assertEqual(sink.marker_patterns, MARKER_PATTERNS)
        self.assertTrue(sink.is_marker(f"latest/{key}"))
        self.assertTrue(sink.is_marker(f"{key}/1/complete.json"))
        self.assertFalse(sink.is_marker(f"{key}/1/grid/data"))


class AreaKeyTests(TestCase):
    """``{org}.{area}``, and why the separator is a dot.

    ``GetGridInfo`` (`forti-internalformat/client.go:150`) splits every key on
    ``/`` and skips anything that is not exactly four parts. A key of
    ``{org}/{area}/{version}/{grid}/latitude`` is five, so every grid would be
    skipped and the dataset would load with no grids and no error at all.
    """

    def test_the_key_carries_the_organisation_in_one_segment(self):
        publication = make_publication(make_collection(), area="ecmwf-ifs")

        self.assertEqual(publication.area_key, f"{publication.organisation.slug}.ecmwf-ifs")
        self.assertNotIn("/", publication.area_key)

    def test_a_version_prefix_splits_into_exactly_what_the_reader_expects(self):
        publication = make_publication(make_collection())

        key = f"{publication.version_prefix(178891200015)}/deadbeef/latitude"

        self.assertEqual(len(key.split("/")), 4)

    def test_two_organisations_may_publish_the_same_area_name(self):
        """Which is the point of carrying the organisation in the key: the name
        is only unique per organisation, and the key is unique on the instance."""
        first = make_publication(make_collection(), area="ecmwf-ifs")
        second = make_publication(make_collection(org_slug="other-org"), area="ecmwf-ifs")

        self.assertNotEqual(first.area_key, second.area_key)

    def test_an_area_key_can_never_be_one_of_the_documents_beside_it(self):
        """The whole of retention's tenancy safety under a shared root. Core
        refuses ``delete_prefix("")`` and can refuse nothing else, because which
        names under the root are areas is this plugin's grammar."""
        publication = make_publication(make_collection(), area="latest")

        self.assertIn(".", publication.area_key)
        for reserved in ("latest", "config", "status", "jsonformat.json"):
            with self.subTest(reserved=reserved):
                self.assertNotEqual(publication.area_key, reserved)


class ExtentSeedingTests(TestCase):
    def test_seeding_from_a_catalog_with_no_boundary_does_nothing(self):
        publication = make_publication(make_collection())

        self.assertFalse(publication.seed_extent_from_catalog())
