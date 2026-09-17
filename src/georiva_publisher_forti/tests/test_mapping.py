"""The slots a publication fills, and the rule that fills them by default.

Which GeoRiva variable feeds which Forti parameter used to be an exact slug
match hidden inside the planner, so a collection that named its variables
anything else could not publish at all and the only way to find out was to
publish and read the refusal. It is data now: one row per slot.

Two things are worth testing without a database. The **vocabulary** — eight
slots, derived from the parameter map rather than typed out again beside it, so
a parameter added with a new source cannot leave a slot nobody can fill. And the
**auto-match**, which is the old hard-coded rule kept as a default: a migration
and a fresh publication both lean on it, and a test that reached it through
migration mechanics would be testing Django rather than the rule.
"""

from django.core.exceptions import ValidationError
from django.db.models.deletion import RestrictedError
from django.db.utils import IntegrityError
from django.test import SimpleTestCase, TestCase

from georiva.core.models import Unit
from georiva_publisher_forti import parameters as params
from georiva_publisher_forti.mapping import auto_match
from georiva_publisher_forti.models import FortiPublication, FortiVariableMapping

from .factories import make_collection, make_publication


class Named:
    """The only thing :func:`auto_match` reads off a variable."""

    def __init__(self, slug):
        self.slug = slug

    def __repr__(self):
        return f"Named({self.slug!r})"


class SlotVocabularyTests(SimpleTestCase):
    def test_there_are_eight_slots(self):
        """Seven source-backed parameters plus ``tp``, which only derivations
        read. The other eight parameters are derived and have no source to map."""
        self.assertEqual(len(params.SLOTS), 8)

    def test_every_slot_a_parameter_reads_directly_is_in_the_vocabulary(self):
        direct = {parameter.source for parameter in params.ALL_PARAMETERS if parameter.source}

        self.assertTrue(direct <= set(params.SLOT_KEYS))

    def test_a_variable_read_only_by_a_derivation_is_still_a_slot(self):
        """``tp`` is published by no parameter directly and by four through a
        derivation. It is as mappable as any other."""
        self.assertIn("tp", params.SLOT_KEYS)
        self.assertNotIn("tp", {parameter.source for parameter in params.ALL_PARAMETERS})

    def test_a_derived_parameter_contributes_no_slot(self):
        """There is nothing to map: the value is computed after the transpose."""
        self.assertNotIn("relative_humidity_2m", params.SLOT_KEYS)
        self.assertNotIn("weather_symbol", params.SLOT_KEYS)

    def test_every_slot_declares_the_unit_it_expects(self):
        self.assertTrue(all(slot.units for slot in params.SLOTS))

    def test_a_slot_names_the_parameters_that_read_it(self):
        """What the slot is *for*, so a surface can say what a wrong mapping
        would spoil without knowing the parameter map itself."""
        temperature = params.BY_SLOT["2t"]

        self.assertIn("air_temperature_2m", temperature.feeds)
        self.assertIn("relative_humidity_2m", temperature.feeds)
        self.assertIn("air_temperature_2m_max6h", temperature.feeds)

    def test_the_keys_are_the_vocabulary_in_order(self):
        self.assertEqual(params.SLOT_KEYS, tuple(slot.key for slot in params.SLOTS))
        self.assertEqual(set(params.BY_SLOT), set(params.SLOT_KEYS))

    def test_the_choices_offer_exactly_the_vocabulary(self):
        self.assertEqual([value for value, _ in params.SLOT_CHOICES], list(params.SLOT_KEYS))


class AutoMatchTests(SimpleTestCase):
    def test_a_conventionally_named_collection_fills_every_slot(self):
        variables = [Named(key) for key in params.SLOT_KEYS]

        matched = auto_match(variables)

        self.assertEqual(set(matched), set(params.SLOT_KEYS))
        self.assertTrue(all(matched[key].slug == key for key in params.SLOT_KEYS))

    def test_an_unrecognised_name_leaves_its_slot_blank(self):
        """Blank rather than absent: the slot exists whether or not anything
        fills it, and a blank one is what an operator is asked to fill in."""
        variables = [Named(key) for key in params.SLOT_KEYS if key != "tcc"]
        variables.append(Named("total-cloud-cover"))

        matched = auto_match(variables)

        self.assertIsNone(matched["tcc"])
        self.assertEqual(matched["2t"].slug, "2t")

    def test_every_slot_is_answered_even_by_an_empty_collection(self):
        matched = auto_match([])

        self.assertEqual(set(matched), set(params.SLOT_KEYS))
        self.assertTrue(all(variable is None for variable in matched.values()))

    def test_a_variable_that_fills_no_slot_is_ignored(self):
        matched = auto_match([Named("2t"), Named("sea_surface_temperature")])

        self.assertEqual(matched["2t"].slug, "2t")
        self.assertNotIn("sea_surface_temperature", matched)


class SeedingTests(TestCase):
    """A publication comes into the world with its eight slots already there.

    Seeding by auto-match is what keeps the change invisible to every
    conventionally-named collection: the mapping a new publication starts with
    is exactly the resolution rule it had before there was a mapping at all.
    """

    def setUp(self):
        self.collection = make_collection()

    def test_a_new_publication_has_a_row_for_every_slot(self):
        publication = make_publication(self.collection)

        self.assertEqual(
            list(publication.variable_mappings.values_list("slot", flat=True).order_by("slot")),
            sorted(params.SLOT_KEYS),
        )

    def test_the_rows_are_filled_by_slug_where_the_collection_agrees(self):
        publication = make_publication(self.collection)

        self.assertEqual(publication.mapped_variables()["2t"].slug, "2t")

    def test_a_slot_the_collection_cannot_fill_is_seeded_blank(self):
        self.collection.variables.filter(slug="tcc").delete()

        publication = make_publication(self.collection)

        self.assertIsNone(publication.mapped_variables()["tcc"])
        self.assertEqual(publication.unmapped_slots(), ["tcc"])

    def test_seeding_does_not_count_as_a_configuration_change(self):
        """Eight rows appearing at creation is the publication being born, not
        its bytes changing: a generation raised here would publish the first run
        under a stamp that says it is already a correction."""
        publication = make_publication(self.collection)

        self.assertEqual(publication.generation, 0)

    def test_a_fully_mapped_publication_reads_as_ready(self):
        publication = make_publication(self.collection)

        self.assertEqual(publication.unmapped_slots(), [])


class MappingConstraintTests(TestCase):
    def setUp(self):
        self.collection = make_collection()
        self.publication = make_publication(self.collection)

    def test_one_row_per_slot_per_publication(self):
        with self.assertRaises(IntegrityError):
            FortiVariableMapping.objects.create(
                publication=self.publication,
                slot="2t",
                variable=self.collection.variables.get(slug="2t"),
            )

    def test_deleting_a_mapped_variable_is_refused(self):
        """A dangling mapping would be discovered at the next publish, by which
        time the operator who deleted the variable is somewhere else."""
        with self.assertRaises(RestrictedError):
            self.collection.variables.get(slug="2t").delete()

    def test_deleting_an_unmapped_variable_is_allowed(self):
        self.publication.variable_mappings.filter(slot="2t").update(variable=None)

        self.collection.variables.get(slug="2t").delete()

        self.assertFalse(self.collection.variables.filter(slug="2t").exists())

    def test_deleting_the_collection_takes_the_whole_publication_with_it(self):
        """The refusal is about losing a variable out from under a live mapping,
        not about the mapping outliving everything it describes. A collection
        deletion cascades to the publication and its rows, so the variable is
        not being stranded — which is exactly the case RESTRICT admits and
        PROTECT would not."""
        self.collection.delete()

        self.assertFalse(FortiPublication.objects.exists())
        self.assertFalse(FortiVariableMapping.objects.exists())

    def test_which_publications_read_a_variable_is_a_query(self):
        variable = self.collection.variables.get(slug="2t")

        self.assertEqual(
            [row.publication_id for row in variable.forti_slots.all()],
            [self.publication.pk],
        )

    def test_a_variable_from_another_collection_is_refused(self):
        other = make_collection(slug="other-surface")
        row = self.publication.variable_mappings.get(slot="2t")
        row.variable = other.variables.get(slug="2t")

        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()

        self.assertIn("variable", ctx.exception.error_dict)

    def test_a_unit_that_disagrees_with_the_slot_cannot_be_saved(self):
        kelvin, _ = Unit.objects.get_or_create(name="Kelvin", defaults={"symbol": "K"})
        variable = self.collection.variables.get(slug="2d")
        variable.unit = kelvin
        variable.save(update_fields=["unit"])
        row = self.publication.variable_mappings.get(slot="2t")
        row.variable = variable

        with self.assertRaises(ValidationError) as ctx:
            row.full_clean()

        self.assertIn("variable", ctx.exception.error_dict)

    def test_a_blank_slot_saves(self):
        row = self.publication.variable_mappings.get(slot="2t")
        row.variable = None

        row.full_clean()
        row.save()

        self.assertEqual(self.publication.unmapped_slots(), ["2t"])


class ConfigurationChangeTests(TestCase):
    """Changing a mapping changes the bytes, and the reader has to be told.

    ``rawdataforecaster`` reloads only on a strictly greater version, and a
    remap moves nothing the run knows about — so without the generation the
    corrected bytes land under the integer the reader already holds and are
    never loaded, with every surface reporting agreement.
    """

    def setUp(self):
        self.collection = make_collection()
        self.publication = make_publication(self.collection)
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            status=FortiPublication.Status.READY,
            published_version=1,
        )
        self.publication.refresh_from_db()

    def _remap(self, slot="2t", variable=None):
        row = self.publication.variable_mappings.get(slot=slot)
        row.variable = variable
        row.save()
        self.publication.refresh_from_db()

    def test_changing_a_mapping_raises_the_generation(self):
        self._remap()

        self.assertEqual(self.publication.generation, 1)

    def test_changing_a_mapping_asks_for_a_republish(self):
        """A raised generation nothing rebuilds is a correction that never
        reaches the bucket: the publication has to become buildable again."""
        self._remap()

        self.assertEqual(self.publication.status, FortiPublication.Status.STALE)

    def test_deleting_a_mapping_row_is_a_configuration_change_too(self):
        self.publication.variable_mappings.get(slot="2t").delete()
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.generation, 1)

    def test_each_change_counts(self):
        self._remap()
        self._remap(variable=self.collection.variables.get(slug="2t"))

        self.assertEqual(self.publication.generation, 2)

    def test_saving_a_row_that_did_not_change_counts_for_nothing(self):
        """The editor posts all eight rows at every submit. Counting saves
        rather than changes would raise the generation by eight a submit
        against a ceiling that refuses to publish — twelve idle submits inside
        one run window and the model could not publish until the next run."""
        for row in self.publication.variable_mappings.all():
            row.save()
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.generation, 0)
        self.assertEqual(self.publication.status, FortiPublication.Status.READY)

    def test_a_row_created_blank_still_counts(self):
        """The slot was unanswered a moment ago and is answered now — by
        nothing, which is an answer the planner acts on."""
        self.publication.variable_mappings.filter(slot="2t").delete()
        self.publication.refresh_from_db()
        before = self.publication.generation

        FortiVariableMapping.objects.create(publication=self.publication, slot="2t", variable=None)
        self.publication.refresh_from_db()

        self.assertEqual(self.publication.generation, before + 1)
