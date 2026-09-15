"""What a publication refuses to be, and what it takes from its collection.

Every one of these fails silently rather than loudly if it is not checked here.

Two models of one organisation sharing a slug means one of them is unreachable —
and a *missing* ``latest/<area key>`` reads as version **0** to an unfixed
reader, so ``rawdataforecaster`` goes looking for ``<area key>/0/complete.json``,
which is fatal at startup and merely logged afterwards, in an error that does not
mention the area at all (`forecast.go:143`).

Renaming a published model is the same failure a step later: the keys already on
the bucket carry the old slug, so the pointer nothing prunes goes on being served
under the old name while the new one has nothing under it.

A non-forecast collection is refused for a reason one step further back than
either: only forecast collections have run boundaries at all, so nothing ever
creates a ``RunIngestion`` for one (`ingestion/models.py`, ``record_file``). A
publication over one is not a publication that fails — it is one that waits
forever for a run that no ingestion will ever open, and says so in a build log
nothing renders.

Visibility replaces D12's flat refusal of anything but a public collection (D18).
That rule's reason was about the *reader* — it holds no credential, so there is
nobody to check a private collection against — and the gate is now the Django
view, which does know who is asking. What is left to enforce is that a
publication is never more open than the collection under it, because Forti would
then serve what nothing else on the instance serves.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from georiva.core.models import Collection
from georiva.core.publishing import PublicationSink
from georiva.ingestion.models import RunIngestion
from georiva_publisher_forti.models import MARKER_PATTERNS, SINK_ROOT, FortiPublication

from .factories import make_collection, make_publication


class ValidationTests(TestCase):
    def test_a_public_collection_is_publishable(self):
        publication = make_publication(make_collection())

        publication.full_clean()

    def test_a_private_collection_is_publishable_privately(self):
        """D12 refused this outright. The reason was the reader's — it holds no
        credential — and the gate is now the view, which knows who is asking."""
        collection = make_collection(visibility=Collection.Visibility.PRIVATE)
        publication = make_publication(collection)

        publication.full_clean()

        self.assertEqual(publication.visibility, FortiPublication.Visibility.PRIVATE)

    def test_an_internal_collection_is_refused(self):
        """Not a dataset with a small audience — a derivation intermediate, with
        no audience to narrow to."""
        collection = make_collection(visibility=Collection.Visibility.INTERNAL)
        publication = make_publication(collection)

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("collection", ctx.exception.message_dict)

    def test_a_non_forecast_collection_is_refused(self):
        """The one condition that is a hard gate rather than a readiness
        finding: no ingestion opens a run for a collection that is not a
        forecast, so there is nothing for this publication to ever transpose."""
        collection = make_collection(is_forecast=False)
        publication = make_publication(collection)

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("collection", ctx.exception.message_dict)

    def test_the_refusal_says_what_to_do_about_it(self):
        """The failure this replaces was ``NothingToPublish: has no closed run``
        — true, and about the run rather than the choice that caused it. The
        refusal has to name the collection's own setting, which is the thing an
        operator can actually change."""
        publication = make_publication(make_collection(is_forecast=False))

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        (message,) = ctx.exception.message_dict["collection"]
        self.assertIn("forecast", message.lower())

    def test_a_forecast_collection_with_no_run_is_still_publishable(self):
        """Configuring ahead of ingestion stays possible. ``is_forecast`` is
        declarative and says nothing about whether a collection *can* publish
        yet; everything that answers that is readiness, reported and not
        refused."""
        collection = make_collection()
        self.assertFalse(RunIngestion.objects.filter(collection=collection).exists())

        make_publication(collection).full_clean()

    def test_two_models_of_one_organisation_may_not_share_a_slug(self):
        first = make_publication(make_collection(slug="global"), slug="kenya")
        second = FortiPublication(
            collection=make_collection(slug="national"),
            slug="kenya",
            west=1,
            south=1,
            east=2,
            north=2,
        )
        second.collection.catalog.organisation = first.collection.catalog.organisation
        second.collection.catalog.save()

        with self.assertRaises(ValidationError) as ctx:
            second.full_clean()

        self.assertIn("slug", ctx.exception.message_dict)

    def test_a_backwards_extent_is_refused(self):
        publication = make_publication(make_collection(), bbox=(42.0, -5.0, 33.0, 6.0))

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("east", ctx.exception.message_dict)


class SlugTests(TestCase):
    """The name a consumer asks by, and a segment of every storage key."""

    def test_a_blank_slug_takes_the_catalogs(self):
        """``Catalog``'s docstring is "a data source that produces multiple
        collections. Examples: GFS, CHIRPS, ERA5, MSG" — which is a model, and
        its slug is already unique per organisation (D17)."""
        collection = make_collection()
        publication = make_publication(collection, slug="")

        publication.full_clean()

        self.assertEqual(publication.slug, collection.catalog.slug)

    def test_a_blank_slug_is_filled_even_without_validation(self):
        """``objects.create`` skips ``full_clean``, and a nameless publication
        would publish under ``{org}.`` — a key nothing asks for."""
        collection = make_collection()

        publication = make_publication(collection, slug="")

        self.assertEqual(publication.slug, collection.catalog.slug)

    def test_an_unpublished_model_may_still_be_renamed(self):
        """Nothing carries the name yet, so nothing is stranded by changing it."""
        publication = make_publication(make_collection(), slug="kenya")

        publication.slug = "ecmwf-ifs"
        publication.full_clean()
        publication.save()

        self.assertEqual(FortiPublication.objects.get(pk=publication.pk).slug, "ecmwf-ifs")

    def test_a_published_model_may_not_be_renamed(self):
        publication = make_publication(make_collection(), slug="kenya", published_version=178891200000)

        publication.slug = "ecmwf-ifs"

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("slug", ctx.exception.message_dict)

    def test_the_rename_is_refused_without_validation_too(self):
        """The same rule and the same reason as ``Organisation.slug``: a rename
        is a storage fact, not a validation nicety, so a path that skips
        ``full_clean()`` must not be able to sneak one through."""
        publication = make_publication(make_collection(), slug="kenya", published_version=178891200000)

        publication.slug = "ecmwf-ifs"

        with self.assertRaises(ValidationError):
            publication.save(update_fields=["slug"])

    def test_publishing_reads_the_row_rather_than_the_instance_in_hand(self):
        """Every build transition is a queryset ``update()``, so an in-memory
        publication's ``published_version`` is stale by exactly the transition
        that makes the slug immutable."""
        publication = make_publication(make_collection(), slug="kenya")
        FortiPublication.objects.filter(pk=publication.pk).update(published_version=178891200000)

        self.assertIsNone(publication.published_version)
        publication.slug = "ecmwf-ifs"

        with self.assertRaises(ValidationError):
            publication.full_clean()


class VisibilityTests(TestCase):
    """D18: defaulting from the collection, never more open than it."""

    def test_it_defaults_to_the_collections(self):
        publication = make_publication(make_collection())

        self.assertEqual(publication.visibility, FortiPublication.Visibility.PUBLIC)

    def test_a_public_collection_may_be_published_privately(self):
        """Why the field exists at all: an NMHS may reasonably serve maps
        publicly and point forecasts to members only, which deriving the tier
        from the collection cannot express."""
        publication = make_publication(
            make_collection(),
            visibility=FortiPublication.Visibility.PRIVATE,
        )

        publication.full_clean()

        self.assertEqual(publication.visibility, FortiPublication.Visibility.PRIVATE)

    def test_a_private_collection_may_not_be_published_publicly(self):
        collection = make_collection(visibility=Collection.Visibility.PRIVATE)
        publication = make_publication(collection, visibility=FortiPublication.Visibility.PUBLIC)

        with self.assertRaises(ValidationError) as ctx:
            publication.full_clean()

        self.assertIn("visibility", ctx.exception.message_dict)

    def test_internal_is_not_a_tier_a_publication_can_hold(self):
        """Refused by never having been offered, which is stronger than a check:
        there is no form and no admin through which it can be chosen."""
        self.assertNotIn("internal", FortiPublication.Visibility.values)


class SinkTests(TestCase):
    def test_two_organisations_publish_into_one_prefix(self):
        """One rawdataforecaster for the instance, given one prefix, listing
        every organisation's areas under it (D14/D15)."""
        first = make_publication(make_collection(slug="global"), slug="global")
        second = make_publication(make_collection(org_slug="other-org"), slug="national")

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
        publication = make_publication(make_collection(), slug="ecmwf-ifs")

        self.assertEqual(publication.area_key, f"{publication.organisation.slug}.ecmwf-ifs")
        self.assertNotIn("/", publication.area_key)

    def test_a_version_prefix_splits_into_exactly_what_the_reader_expects(self):
        publication = make_publication(make_collection())

        key = f"{publication.version_prefix(178891200015)}/deadbeef/latitude"

        self.assertEqual(len(key.split("/")), 4)

    def test_two_organisations_may_publish_the_same_area_name(self):
        """Which is the point of carrying the organisation in the key: the name
        is only unique per organisation, and the key is unique on the instance."""
        first = make_publication(make_collection(), slug="ecmwf-ifs")
        second = make_publication(make_collection(org_slug="other-org"), slug="ecmwf-ifs")

        self.assertNotEqual(first.area_key, second.area_key)

    def test_an_area_key_can_never_be_one_of_the_documents_beside_it(self):
        """The whole of retention's tenancy safety under a shared root. Core
        refuses ``delete_prefix("")`` and can refuse nothing else, because which
        names under the root are areas is this plugin's grammar."""
        publication = make_publication(make_collection(), slug="latest")

        self.assertIn(".", publication.area_key)
        for reserved in ("latest", "config", "status", "jsonformat.json"):
            with self.subTest(reserved=reserved):
                self.assertNotEqual(publication.area_key, reserved)


class ExtentSeedingTests(TestCase):
    def test_seeding_from_a_catalog_with_no_boundary_does_nothing(self):
        publication = make_publication(make_collection())

        self.assertFalse(publication.seed_extent_from_catalog())
