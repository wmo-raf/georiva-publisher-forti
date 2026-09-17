"""The publication form: what it offers, what it refuses, and what it admits.

Two subjects, and they are the same subject seen twice.

**The chooser** is narrowed and the queryset behind it is not, so the two halves
of the forecast rule stay distinct: the dropdown is the hint an operator meets
before choosing, and :meth:`FortiPublication.clean` is the refusal they read if
they choose anyway. A form field that rejects a value excludes it from model
validation, so narrowing the queryset would have replaced that refusal with
"Select a valid choice" and left the model's sentence unreachable from here.

The organisation rule is the opposite shape and is asserted beside it. A foreign
collection *is* narrowed out of the queryset, and has to be turned away as absent
rather than explained — a caller must not learn from a refusal that the row
exists and is a perfectly good forecast belonging to somebody else.

**The mapping editor** is the same distinction drawn three ways instead of two,
because the eight slots admit of three answers rather than two. A unit that
disagrees is **refused**, and the refusal names the parameters that would have
carried the wrong number. A mapping that is merely suspicious — one variable in
two slots, a declared range that cannot reach the slot's — **warns**, and is
saved once the warning is acknowledged. And everything else is **admitted**: the
form states, on the page, that it has not checked whether a variable is the
quantity its slot names, because nothing can.

That last one is the reason this module is worth its length. An operator who
reads a saved mapping as a verified mapping has been misled by the surface, and
a test suite that asserted the two checks while letting the page imply a third
would be holding the feature to the wrong claim.
"""

from django.test import TestCase
from django.urls import reverse

from georiva.core.models import Unit, Variable
from georiva.organisations.testing import dial_org
from georiva_publisher_forti import forms
from georiva_publisher_forti import parameters as params
from georiva_publisher_forti.models import NOT_A_FORECAST, FortiPublication

from .factories import make_collection, make_publication, make_user

ADD_URL = "wagtailsnippets_georiva_publisher_forti_fortipublication:add"
EDIT_URL = "wagtailsnippets_georiva_publisher_forti_fortipublication:edit"

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


class MappingEditorMixin:
    """One publication, already seeded by auto-match, and a way to re-post it.

    Every test below edits a publication rather than creating one, because the
    mapping is only editable once there is a collection to draw variables from —
    which is the whole reason the section is absent from the add form.
    """

    def setUp(self):
        dial_org(self.client)
        self.client.force_login(make_user("mapper", superuser=True))
        self.collection = make_collection()
        self.publication = make_publication(self.collection)
        self.url = reverse(EDIT_URL, args=[self.publication.pk])

    def variable(self, slug):
        return self.collection.variables.get(slug=slug)

    def payload(self, acknowledge=False, **overrides):
        """The whole form, with the mapping as it currently stands.

        Whole rather than partial because that is what a browser posts, and the
        thing most easily got wrong here — a generation raised eight times by a
        submit that changed nothing — is only visible when every row is present.
        """
        mapped = self.publication.mapped_variables()
        body = {
            "collection": self.publication.collection_id,
            "slug": self.publication.slug,
            "visibility": self.publication.visibility,
            "is_enabled": "on",
            "generation": self.publication.generation,
            **EXTENT,
        }
        for key, variable in mapped.items():
            body[forms.field_name(key)] = variable.pk if variable else ""
        if acknowledge:
            body[forms.ACKNOWLEDGE_FIELD] = "on"
        for key, value in overrides.items():
            body[key] = value
        return body

    def post(self, **kwargs):
        return self.client.post(self.url, self.payload(**kwargs))

    def remap(self, slot, variable, **kwargs):
        return self.post(**{forms.field_name(slot): variable.pk if variable else "", **kwargs})

    def stored(self, slot):
        return self.publication.mapped_variables()[slot]


class MappingRenderTests(MappingEditorMixin, TestCase):
    def test_the_form_renders_the_eight_fixed_slots(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        for key in params.SLOT_KEYS:
            self.assertIn(forms.field_name(key), response.context["form"].fields)

    def test_it_renders_those_eight_and_no_more(self):
        """Fixed, not a list an operator adds to: an add-and-remove inline panel
        would offer a shape the mapping cannot take."""
        response = self.client.get(self.url)
        mapping_fields = [name for name in response.context["form"].fields if name.startswith(forms.SLOT_FIELD_PREFIX)]

        self.assertEqual(len(mapping_fields), len(params.SLOT_KEYS))

    def test_each_slot_is_prefilled_by_slug_auto_match(self):
        """The common case asks for no decisions, so editing is purely an
        override rather than eight choices nobody wanted to make."""
        form = self.client.get(self.url).context["form"]

        self.assertEqual(form.initial[forms.field_name("2t")], self.variable("2t").pk)

    def test_only_the_collections_own_variables_are_offered(self):
        elsewhere = make_collection(slug="other-surface")

        form = self.client.get(self.url).context["form"]
        offered = set(form.fields[forms.field_name("2t")].queryset)

        self.assertIn(self.variable("2t"), offered)
        self.assertNotIn(elsewhere.variables.get(slug="2t"), offered)

    def test_a_blank_slot_is_shown_as_incomplete(self):
        self.publication.variable_mappings.filter(slot="tcc").update(variable=None)

        response = self.client.get(self.url)

        self.assertContains(response, forms.INCOMPLETE_LABEL)

    def test_a_fully_mapped_publication_is_not_shown_as_incomplete(self):
        self.assertNotContains(self.client.get(self.url), forms.INCOMPLETE_LABEL)

    def test_the_add_form_has_no_mapping(self):
        """There is no collection yet, so there are no variables to choose
        between; creation seeds the eight rows by auto-match and the operator
        edits them afterwards."""
        form = self.client.get(reverse(ADD_URL)).context["form"]

        self.assertEqual([name for name in form.fields if name.startswith(forms.SLOT_FIELD_PREFIX)], [])


class WhatTheFormAdmitsTests(MappingEditorMixin, TestCase):
    """The claim the page makes about itself, which is the feature's honesty.

    Asserted as its own subject rather than folded into a render test, because
    it is the one thing here that would go unnoticed if it quietly stopped being
    rendered: the checks would still work, and the page would still look right.
    """

    def test_the_page_states_what_it_checked(self):
        response = self.client.get(self.url)

        for sentence in forms.CHECKS_MADE:
            self.assertContains(response, sentence)

    def test_the_page_states_what_it_did_not_check(self):
        response = self.client.get(self.url)

        for sentence in forms.CHECKS_NOT_MADE:
            self.assertContains(response, sentence)

    def test_it_names_the_confusion_no_check_can_catch(self):
        """Dew point in the air-temperature slot, by name. A page that said only
        "some checks are not made" would be true and useless."""
        self.assertContains(self.client.get(self.url), "Dew point mapped into the")


class UnitRefusalTests(MappingEditorMixin, TestCase):
    """The one hard refusal, met through the form rather than through save().

    Forti copies units out of ``meta.json`` without interpreting them, so a
    slot whose variable is in kelvin publishes a number wrong by 273 under a
    label that says celsius, and every layer downstream agrees.
    """

    def setUp(self):
        super().setUp()
        # A ninth variable rather than a retuned one of the eight. Retuning a
        # variable that already fills a slot refuses *that* slot too, which is
        # correct and would hide whether the refusal lands where it was aimed.
        kelvin, _ = Unit.objects.get_or_create(symbol="K", defaults={"name": "Kelvin"})
        self.kelvin_variable = Variable.objects.create(
            collection=self.collection,
            slug="t2m-kelvin",
            name="t2m-kelvin",
            unit=kelvin,
            value_min=-100,
            value_max=2000,
        )

    def test_a_unit_that_disagrees_with_its_slot_cannot_be_saved(self):
        response = self.remap("2t", self.kelvin_variable)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored("2t"), self.variable("2t"))

    def test_the_refusal_says_what_would_have_been_published(self):
        """Not "invalid unit" — the parameters that would have carried it, which
        is what an operator would otherwise have found out from a consumer."""
        response = self.remap("2t", self.kelvin_variable)
        (message,) = response.context["form"].errors[forms.field_name("2t")]

        self.assertIn("air_temperature_2m", message)
        self.assertIn("degC", message)

    def test_the_refusal_lands_on_the_slot_it_is_about(self):
        response = self.remap("2t", self.kelvin_variable)

        self.assertNotIn(forms.field_name("2d"), response.context["form"].errors)

    def test_a_unit_spelled_differently_is_the_same_unit(self):
        """``°C`` and ``degC`` are one unit and kelvin is not, which is why the
        comparison goes through pint rather than through string equality."""
        degrees, _ = Unit.objects.get_or_create(symbol="°C", defaults={"name": "Degrees Celsius"})
        variable = self.variable("2t")
        variable.unit = degrees
        variable.save(update_fields=["unit"])

        response = self.remap("2d", variable, acknowledge=True)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored("2d"), variable)


class WarningTests(MappingEditorMixin, TestCase):
    """The two suspicions, which block nothing and are not therefore silent.

    Both are legal mappings. What makes them worth raising is that each is also
    what a particular mistake looks like — and what makes them warnings rather
    than refusals is that neither is evidence enough to overrule an operator who
    means it.
    """

    def test_a_variable_filling_two_slots_is_not_saved_unacknowledged(self):
        response = self.remap("2d", self.variable("2t"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored("2d"), self.variable("2d"))

    def test_the_warning_is_shown_rather_than_a_field_refusal(self):
        """On the acknowledgement, not on the slot: a refusal on the slot would
        say the mapping is wrong, and it is not — it is unusual."""
        response = self.remap("2d", self.variable("2t"))
        form = response.context["form"]

        self.assertNotIn(forms.field_name("2d"), form.errors)
        self.assertIn(forms.ACKNOWLEDGE_FIELD, form.errors)

    def test_the_warning_names_both_slots_and_what_they_would_publish(self):
        response = self.remap("2d", self.variable("2t"))

        self.assertContains(response, "dew_point_temperature_2m")

    def test_an_acknowledged_shared_variable_saves(self):
        response = self.remap("2d", self.variable("2t"), acknowledge=True)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored("2d"), self.variable("2t"))

    def test_a_declared_range_that_cannot_reach_the_slot_warns(self):
        """Kelvin numbers under a celsius unit row: the unit check agrees,
        because the unit row is what it reads."""
        variable = self.variable("2t")
        variable.value_min, variable.value_max = 200.0, 320.0
        variable.save(update_fields=["value_min", "value_max"])

        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertIn(forms.ACKNOWLEDGE_FIELD, response.context["form"].errors)

    def test_an_acknowledged_odd_range_saves(self):
        variable = self.variable("2t")
        variable.value_min, variable.value_max = 200.0, 320.0
        variable.save(update_fields=["value_min", "value_max"])

        self.assertEqual(self.post(acknowledge=True).status_code, 302)

    def test_an_ordinary_mapping_needs_no_acknowledgement(self):
        """The acknowledgement is not a box on every save — a page that asked
        for one unconditionally would be ticked without being read."""
        self.assertEqual(self.post().status_code, 302)

    def test_a_blank_slot_saves_and_asks_for_nothing(self):
        """A publication may be configured while its collection is still
        declaring its variables; that is incomplete, not suspicious."""
        response = self.remap("tcc", None)

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.stored("tcc"))


class GenerationTests(MappingEditorMixin, TestCase):
    """What the editor costs the publication's version, which has a ceiling.

    The generation is the low two digits of the published version and refuses to
    publish at ninety-nine. The editor posts all eight rows at every submit, so
    a form that counted submits rather than changes would spend the budget on
    doing nothing.
    """

    def test_a_submit_that_changed_no_mapping_raises_nothing(self):
        self.post()
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.generation, 0)

    def test_a_remap_raises_the_generation_once(self):
        self.remap("tcc", None)
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.generation, 1)

    def test_a_remap_asks_for_a_republish(self):
        """A raised generation nothing rebuilds is a correction that never
        reaches the bucket."""
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            status=FortiPublication.Status.READY,
            published_version=1,
        )

        self.remap("tcc", None)
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.status, FortiPublication.Status.STALE)


class CollectionChangeTests(MappingEditorMixin, TestCase):
    """A filled mapping is the collection's, and cannot follow it elsewhere.

    The eight rows name variables of one collection and the model refuses a
    variable from another, so a publication re-pointed at a second collection
    holds eight rows that cannot be saved and cannot be published — a state
    reachable before this editor existed and invisible until the next run.
    """

    def test_repointing_a_filled_publication_is_refused(self):
        elsewhere = make_collection(slug="other-surface")

        response = self.post(collection=elsewhere.pk)

        self.assertEqual(response.status_code, 200)
        self.assertIn("collection", response.context["form"].errors)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.collection, self.collection)

    def test_repointing_an_unmapped_publication_is_allowed(self):
        """Configured ahead of the data and pointed at the wrong collection is
        an ordinary mistake with nothing yet to strand."""
        elsewhere = make_collection(slug="other-surface")
        self.publication.variable_mappings.update(variable=None)

        response = self.post(collection=elsewhere.pk)

        self.assertEqual(response.status_code, 302)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.collection, elsewhere)
