"""The Area panel: what it offers, what it fills, and that it stops at the lock.

The page's behaviour (picking fills the inputs, the map redraws) is script and is
not asserted here; what is asserted is everything the script is *given* — the
countries, the item footprints and the lock — and the one refusal the server
makes for a request not made from the page.
"""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from georiva.core.models import Item
from georiva.organisations.testing import dial_org
from georiva_publisher_forti.area import COUNTRY_MARGIN, country_extents, recent_item_extents

from .factories import REFERENCE_TIME, make_collection, make_publication, make_user
from .test_admin import EDIT_URL, publication_payload

ITEMS_URL = "forti_area_items"


class CountryTests(SimpleTestCase):
    def test_a_country_comes_with_a_west_south_east_north_box(self):
        by_code = {c["code"]: c for c in country_extents()}

        west, south, east, north = by_code["KE"]["bbox"]

        self.assertLess(west, east)
        self.assertLess(south, north)
        self.assertTrue(33 < west < 35 and 41 < east < 42.5, by_code["KE"])

    def test_the_margin_is_the_one_seeding_uses(self):
        self.assertEqual(COUNTRY_MARGIN, 0.5)


def make_item(collection, hours, bounds):
    return Item.objects.create(collection=collection, time=REFERENCE_TIME + timedelta(hours=hours), bounds=bounds)


class ItemExtentTests(TestCase):
    def setUp(self):
        dial_org(self.client)
        self.collection = make_collection()

    def test_identical_footprints_collapse_into_the_newest(self):
        make_item(self.collection, 0, [30.0, -5.0, 42.0, 6.0])
        make_item(self.collection, 1, [30.0, -5.0, 42.0, 6.0])
        make_item(self.collection, 2, [10.0, 0.0, 20.0, 5.0])

        found = recent_item_extents(self.collection)

        self.assertEqual([item["bbox"] for item in found], [[10.0, 0.0, 20.0, 5.0], [30.0, -5.0, 42.0, 6.0]])

    def test_an_item_without_bounds_is_not_offered(self):
        Item.objects.create(collection=self.collection, time=REFERENCE_TIME)

        self.assertEqual(recent_item_extents(self.collection), [])

    def test_the_endpoint_answers_for_this_organisations_collection(self):
        self.client.force_login(make_user("viewer", superuser=True))
        make_item(self.collection, 0, [30.0, -5.0, 42.0, 6.0])

        response = self.client.get(reverse(ITEMS_URL), {"collection": self.collection.pk})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["bbox"], [30.0, -5.0, 42.0, 6.0])

    def test_the_endpoint_does_not_know_another_organisations_collection(self):
        self.client.force_login(make_user("viewer", superuser=True))
        theirs = make_collection(slug="theirs", org_slug="other-org")
        make_item(theirs, 0, [30.0, -5.0, 42.0, 6.0])

        response = self.client.get(reverse(ITEMS_URL), {"collection": theirs.pk})

        self.assertEqual(response.status_code, 404)

    def test_a_missing_collection_is_a_404_not_an_error(self):
        self.client.force_login(make_user("viewer", superuser=True))

        self.assertEqual(self.client.get(reverse(ITEMS_URL)).status_code, 404)


class LockTests(TestCase):
    def setUp(self):
        dial_org(self.client)
        self.client.force_login(make_user("editor", superuser=True))
        self.collection = make_collection()
        self.publication = make_publication(self.collection)

    def edit(self, **overrides):
        return self.client.post(
            reverse(EDIT_URL, args=[self.publication.pk]),
            publication_payload(self.collection, slug=self.publication.slug, **overrides),
        )

    def test_before_a_grid_exists_the_area_can_be_changed(self):
        response = self.edit(west=31.0)

        self.assertEqual(response.status_code, 302)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.west, 31.0)

    def test_once_a_grid_exists_a_changed_edge_is_refused(self):
        self.publication.grid_id = "dd566ea71ba587cecb5d66213e235d07"
        self.publication.save()
        stored_west = self.publication.west

        response = self.edit(west=stored_west - 1)

        self.assertEqual(response.status_code, 200)
        self.assertIn("west", response.context["form"].errors)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.west, stored_west)

    def test_once_a_grid_exists_the_page_renders_the_edges_read_only(self):
        self.publication.grid_id = "dd566ea71ba587cecb5d66213e235d07"
        self.publication.save()

        response = self.client.get(reverse(EDIT_URL, args=[self.publication.pk]))

        self.assertContains(response, "The area is fixed after the first publish.")
        self.assertTrue(response.context["form"].fields["west"].widget.attrs["readonly"])

    def test_an_unpublished_area_opens_in_custom_with_its_numbers(self):
        response = self.client.get(reverse(EDIT_URL, args=[self.publication.pk]))

        self.assertContains(response, "const saved = true;")
        self.assertContains(response, "const hasErrors = false;")
        self.assertContains(response, 'data-mode="custom"')

    def test_a_refused_area_reopens_with_the_inputs_in_view(self):
        response = self.edit(west=200)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "const hasErrors = true;")
