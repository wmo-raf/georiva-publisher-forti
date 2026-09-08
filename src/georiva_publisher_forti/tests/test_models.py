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
from georiva_publisher_forti.models import MARKER_PATTERNS, SINK_SLUG, FortiPublication

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
    def test_every_area_of_one_organisation_shares_its_prefix(self):
        """One rawdataforecaster per organisation, given one prefix, listing
        every area under it."""
        first = make_publication(make_collection(slug="global"), area="global")
        second = make_publication(make_collection(slug="national"), area="national")
        second.collection.catalog.organisation = first.collection.catalog.organisation
        second.collection.catalog.save()

        self.assertEqual(first.sink().root, second.sink().root)
        self.assertTrue(first.sink().root.endswith(f"/{SINK_SLUG}/"))

    def test_the_prefix_opens_with_the_owning_organisation(self):
        publication = make_publication(make_collection())

        self.assertTrue(publication.sink().root.startswith(f"{publication.organisation.slug}/"))

    def test_the_sink_knows_which_paths_are_markers(self):
        sink = make_publication(make_collection()).sink()

        self.assertEqual(sink.marker_patterns, MARKER_PATTERNS)
        self.assertTrue(sink.is_marker("latest/kenya"))
        self.assertTrue(sink.is_marker("kenya/1/complete.json"))
        self.assertFalse(sink.is_marker("kenya/1/grid/data"))


class ExtentSeedingTests(TestCase):
    def test_seeding_from_a_catalog_with_no_boundary_does_nothing(self):
        publication = make_publication(make_collection())

        self.assertFalse(publication.seed_extent_from_catalog())
