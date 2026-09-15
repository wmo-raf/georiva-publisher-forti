"""What the publication form offers, which is not what it accepts.

The chooser is narrowed and the queryset behind it is not, so the two halves of
the forecast rule stay distinct: the dropdown is the hint an operator meets
before choosing, and :meth:`FortiPublication.clean` is the refusal they read if
they choose anyway. A form field that rejects a value excludes it from model
validation, so narrowing the queryset would have replaced that refusal with
"Select a valid choice" and left the model's sentence unreachable from here.

The organisation rule is the opposite shape and is asserted beside it. A foreign
collection *is* narrowed out of the queryset, and has to be turned away as absent
rather than explained — a caller must not learn from a refusal that the row
exists and is a perfectly good forecast belonging to somebody else.
"""

from django.test import TestCase
from django.urls import reverse

from georiva.organisations.testing import dial_org
from georiva_publisher_forti.models import NOT_A_FORECAST, FortiPublication

from .factories import make_collection, make_user

ADD_URL = "wagtailsnippets_georiva_publisher_forti_fortipublication:add"

EXTENT = {"west": 32.0, "south": 4.0, "east": 33.25, "north": 5.25}


class CollectionChooserTests(TestCase):
    def setUp(self):
        dial_org(self.client)
        self.client.force_login(make_user("org-admin", superuser=True))

    def offered(self):
        """The collections the dropdown actually renders, by primary key."""
        response = self.client.get(reverse(ADD_URL))
        self.assertEqual(response.status_code, 200)
        field = response.context["form"].fields["collection"]
        return [value.value for value, _label in field.choices if value]

    def test_a_non_forecast_collection_is_not_offered(self):
        forecast = make_collection(slug="ifs-surface")
        rainfall = make_collection(slug="rainfall", is_forecast=False)

        offered = self.offered()

        self.assertIn(forecast.pk, offered)
        self.assertNotIn(rainfall.pk, offered)

    def test_another_organisations_forecast_collection_is_not_offered(self):
        """The forecast filter narrows the organisation's collections — it does
        not stand in for the organisation filter, which is applied to the same
        field after this one and rebuilds the choices through it."""
        theirs = make_collection(slug="ifs-surface", org_slug="other-org")

        self.assertNotIn(theirs.pk, self.offered())


class PostedCollectionTests(TestCase):
    """The half a dropdown cannot cover: an id that never came from it."""

    def setUp(self):
        dial_org(self.client)
        self.client.force_login(make_user("poster", superuser=True))

    def post(self, collection):
        return self.client.post(
            reverse(ADD_URL),
            {"collection": collection.pk, "slug": "posted", "visibility": "public", "is_enabled": "on", **EXTENT},
        )

    def test_a_posted_non_forecast_id_is_refused_in_terms_of_the_setting(self):
        response = self.post(make_collection(slug="rainfall", is_forecast=False))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FortiPublication.objects.exists())
        (message,) = response.context["form"].errors["collection"]
        self.assertEqual(message, NOT_A_FORECAST)

    def test_a_posted_foreign_id_is_refused_without_being_explained(self):
        """It is a forecast collection, so the forecast sentence would be a lie —
        and one that confirms the row exists to somebody who may not see it."""
        response = self.post(make_collection(slug="ifs-surface", org_slug="other-org"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FortiPublication.objects.exists())
        (message,) = response.context["form"].errors["collection"]
        self.assertNotEqual(message, NOT_A_FORECAST)
