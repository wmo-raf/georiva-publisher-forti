"""What the publication form offers, which is not the same question as what it
accepts.

``clean()`` is the rule; the chooser is how an operator meets it. Both are here
because either alone is a way to be wrong: a filtered dropdown with no model
validation is a hint that a posted id walks straight past, and model validation
with an unfiltered dropdown is a form that offers choices it will then refuse.

The second assertion is about *composition* rather than about tenancy. The
organisation filter is already applied — every relation field on a Wagtail admin
form is narrowed to the active organisation by ``scope_form_fields`` — and the
forecast filter has to narrow that further rather than replace it. Assigning a
queryset over every forecast collection on the instance would satisfy the first
assertion and hand one organisation another's collection list; nothing else in
this repo would notice.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG, dial_org, join_org
from georiva_publisher_forti.models import NOT_A_FORECAST, FortiPublication

from .factories import make_collection


class CollectionChooserTests(TestCase):
    def setUp(self):
        dial_org(self.client)
        user = get_user_model().objects.create_user(
            username="org-admin",
            email="org-admin@example.org",
            password="not-a-real-password",
            is_staff=True,
            is_superuser=True,
        )
        join_org(user, DEFAULT_TEST_ORG_SLUG)
        self.client.force_login(user)
        self.url = reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:add")

    def offered(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return list(response.context["form"].fields["collection"].queryset)

    def test_a_non_forecast_collection_is_not_offered(self):
        forecast = make_collection(slug="ifs-surface")
        rainfall = make_collection(slug="rainfall", is_forecast=False)

        offered = self.offered()

        self.assertIn(forecast, offered)
        self.assertNotIn(rainfall, offered)

    def test_another_organisations_forecast_collection_is_not_offered(self):
        """The forecast filter narrows the organisation's collections — it does
        not stand in for the organisation filter."""
        theirs = make_collection(slug="ifs-surface", org_slug="other-org")

        self.assertNotIn(theirs, self.offered())


class PostedCollectionTests(TestCase):
    """The half the dropdown cannot cover: an id that never came from it.

    Django's own refusal — "Select a valid choice. That choice is not one of the
    available choices." — is what this form produces without help, because a
    field that rejects a value excludes it from model validation and
    ``clean()``'s sentence never renders. True, and it names neither the rule
    nor the setting that would satisfy it.
    """

    def setUp(self):
        dial_org(self.client)
        user = get_user_model().objects.create_user(
            username="poster",
            email="poster@example.org",
            password="not-a-real-password",
            is_staff=True,
            is_superuser=True,
        )
        join_org(user, DEFAULT_TEST_ORG_SLUG)
        self.client.force_login(user)

    def test_a_posted_non_forecast_id_is_refused_in_terms_of_the_setting(self):
        rainfall = make_collection(slug="rainfall", is_forecast=False)

        response = self.client.post(
            reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:add"),
            {
                "collection": rainfall.pk,
                "slug": "rainfall",
                "visibility": "public",
                "is_enabled": "on",
                "west": 32.0,
                "south": 4.0,
                "east": 33.25,
                "north": 5.25,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FortiPublication.objects.exists())
        (message,) = response.context["form"].errors["collection"]
        self.assertEqual(message, NOT_A_FORECAST)
