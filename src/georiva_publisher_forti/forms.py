"""The publication form, and the eight slots it grew.

The mapping has been data since #12 and editable only from a shell. What this
adds is the surface, and the surface is where the feature's one real difficulty
lives: **three answers, not two.**

A unit that disagrees with its slot is **refused**, because Forti copies units
out of ``meta.json`` without interpreting them and the number published would be
wrong by a constant under a label that looks right.

A mapping that is merely suspicious — one variable in two slots, a declared
range that cannot reach the slot's — **warns**, and saves once the warning is
acknowledged. Refusing either would be a lie: both are things an operator may
legitimately mean, and neither is evidence enough to overrule them.

And everything else is **admitted**, in as many words, on the page. Dew point
mapped into the air-temperature slot is degrees celsius into degrees celsius:
the unit check passes, both warnings stay silent, every layer downstream agrees,
and the forecast is wrong. An operator who reads a saved mapping as a verified
one has been misled by the surface — which is worse than not warning at all, and
is the reason :data:`CHECKS_NOT_MADE` is rendered rather than merely true.

## Why the fields are built rather than written out

Eight ``ModelChoiceField``s typed into a class body would be the slot vocabulary
restated in a second place, and ``parameters.py`` goes to some trouble to derive
that vocabulary from the parameter map precisely so there is no second place. A
slot key is also not a Python identifier — ``2t`` — so the names have to be
derived anyway. :func:`field_name` is that derivation, and the class is assembled
with the fields it produces.

## Why there is no mapping on the add form

A slot chooses between the variables of *this publication's* collection, and on
the add form there is no collection yet. What happens instead is what happened
before this form existed: :meth:`~.models.FortiPublication.seed_mapping` fills
all eight rows by slug auto-match at creation, and the operator edits them
afterwards. So the common case still asks for no decisions at all, and the case
auto-match cannot answer is answered on the page that knows which collection it
is asking about.
"""

from dataclasses import dataclass

from django import forms
from django.core.exceptions import ValidationError
from wagtail.admin.forms import WagtailAdminModelForm

from georiva.core.models import Variable

from . import mapping
from . import parameters as params
from .models import FortiVariableMapping

#: Prefix distinguishing a slot's field from every other field on the form. Not
#: cosmetic: it is what lets the panel, the tests and :func:`field_name` agree on
#: which fields are the mapping without enumerating them.
SLOT_FIELD_PREFIX = "slot_"

#: The tick-box that turns a warning into a decision. Not a model field and not
#: stored: what is acknowledged is *these* warnings at *this* submit, and a
#: stored flag would go on claiming an acknowledgement after the mapping it was
#: given for had been edited away.
ACKNOWLEDGE_FIELD = "acknowledge_mapping_concerns"

#: What a slot nothing fills says beside its chooser. A blank slot is legal —
#: a publication may be configured while its collection is still declaring its
#: variables — so this reads as unfinished rather than as broken, which is also
#: how the planner treats it.
INCOMPLETE_LABEL = "no variable yet"

#: What the chooser's own blank option says. Explicit rather than Django's row
#: of dashes, because leaving a slot blank is a thing an operator may mean.
EMPTY_LABEL = "— not mapped —"

ACKNOWLEDGEMENT_LABEL = "I have read the warnings above and mean this mapping."

ACKNOWLEDGEMENT_REQUIRED = (
    "This mapping is unusual rather than wrong, so nothing here will stop you — but it has to "
    "be a decision rather than an oversight. Read the warnings above and tick the box, or change "
    "the slots they are about."
)

COLLECTION_IS_MAPPED = (
    "This publication already maps {filled} of the {total} slots to variables of {collection!r}, "
    "and a slot may only be filled from its own publication's collection. Moving the publication "
    "would leave every one of those rows naming a variable this publication can no longer read, "
    "which is a state nothing refuses and nothing publishes. Clear the mapping first, or make a "
    "second publication of the other collection."
)

#: What the form is entitled to say it did. Rendered on the page rather than
#: merely being true of the code, because the page is where somebody decides
#: how much the saved mapping is worth.
CHECKS_MADE = (
    "Each variable carries the unit its slot publishes, compared through pint so that °C and "
    "degC agree and kelvin does not. A disagreement is refused outright.",
    "No variable fills two slots, and no declared range sits entirely outside the values its "
    "slot publishes. Either one warns, and saves once acknowledged.",
)

#: What it did not, which is the more important half. Ordered as an argument
#: rather than as a list: the specific gap, then its scope, then what follows
#: for the operator reading a saved mapping.
CHECKS_NOT_MADE = (
    "Whether a variable is the quantity its slot names. Dew point mapped into the "
    "air-temperature slot is degrees celsius into degrees celsius: every check above passes, "
    "every layer downstream agrees, and the forecast served is wrong. Nothing in GeoRiva will "
    "catch it.",
    "Whether the numbers are right. Nothing on this page reads a raster — the checks above are "
    "made against what the collection declares about itself.",
    "So a saved mapping is one you have asserted. It is not one that has been verified.",
)


def field_name(slot_key: str) -> str:
    """The form field a slot is edited through.

    Derived rather than declared because a slot key is not a Python identifier:
    ``2t`` cannot be a keyword argument, and a hand-written mapping from key to
    field name would be the vocabulary restated a third time.
    """
    return f"{SLOT_FIELD_PREFIX}{slot_key}"


class SlotChoiceField(forms.ModelChoiceField):
    """A chooser over one collection's variables, named the way a slot is.

    ``Variable.__str__`` is ``collection:slug``, which is right everywhere a
    variable can come from any collection and is noise in eight dropdowns that
    all offer the same one. What an operator is matching against is the slug —
    it is what auto-match tried and what the slot is named after — so the slug
    leads, and the variable's own name follows it when it says anything the slug
    does not.
    """

    def label_from_instance(self, obj):
        return obj.slug if obj.name == obj.slug else f"{obj.slug} — {obj.name}"


@dataclass(frozen=True)
class Row:
    """One slot as the panel renders it: the role, the chooser, and the doubts."""

    slot: params.Slot
    field: forms.BoundField
    concerns: tuple[mapping.Concern, ...]

    @property
    def incomplete(self) -> bool:
        return not self.field.value()


class VariableMappingMixin:
    """The eight slots, on the form that already decides everything else.

    On the form rather than in a formset because the slot set is *fixed*: there
    is nothing to add and nothing to remove, and a formset would offer both. The
    fields themselves are attached by :func:`_mapping_fields` at the bottom of
    this module, where the vocabulary can be read.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._concerns = None

        if not self.has_mapping:
            # The add form. Removed rather than left empty: a chooser with no
            # collection behind it offers nothing, and a required-looking row of
            # eight blanks would read as eight decisions somebody skipped.
            for name in self.mapping_field_names():
                del self.fields[name]
            return

        variables = self.instance.collection.variables.order_by("slug")
        stored = self.instance.mapped_variables()
        for slot in params.SLOTS:
            field = self.fields[field_name(slot.key)]
            # The collection's own variables and no others, which is also the
            # model's refusal: a publication reads the COGs of its own
            # collection, so a variable from another has no asset at any step.
            field.queryset = variables
            # Rendered by the panel as the row's "Published as" cell, under the
            # id Django's ``aria-describedby`` points at. Set here rather than
            # written into the template so the description a screen reader is
            # given and the one the page shows cannot become two different
            # sentences.
            field.help_text = ", ".join(slot.feeds)
            variable = stored.get(slot.key)
            self.initial[field_name(slot.key)] = variable.pk if variable else None

    # -- what the panel reads ------------------------------------------------

    @property
    def has_mapping(self) -> bool:
        """Whether this form is editing a mapping at all — see the module
        docstring on why the add form is not."""
        return bool(self.instance and self.instance.pk and self.instance.collection_id)

    def mapping_field_names(self) -> list[str]:
        return [field_name(slot.key) for slot in params.SLOTS] + [ACKNOWLEDGE_FIELD]

    def mapping_rows(self) -> tuple[Row, ...]:
        """Every slot, in vocabulary order, with whatever is said about it.

        Assembled here rather than in the template for the reason every other
        surface in this plugin gives: a distinction drawn inside ``{% if %}`` is
        a distinction nobody tests.
        """
        if not self.has_mapping:
            return ()
        raised = self.mapping_concerns()
        return tuple(
            Row(
                slot=slot,
                field=self[field_name(slot.key)],
                concerns=tuple(concern for concern in raised if concern.slot == slot.key),
            )
            for slot in params.SLOTS
        )

    def mapping_concerns(self) -> tuple[mapping.Concern, ...]:
        """The warnings about the mapping as it currently stands.

        On a bound form that is the *posted* mapping, worked out during
        :meth:`clean` and kept — so the page an operator reads back warns about
        what they submitted rather than about what is still stored.
        """
        if self._concerns is None:
            self._concerns = mapping.concerns(self.instance.mapped_variables())
        return self._concerns

    @property
    def acknowledgement(self):
        """The tick-box, when there is something to tick it for.

        Absent when the mapping raises nothing, because a box that appears on
        every save is a box that gets ticked without being read — and the whole
        value of this one is that it is rare.
        """
        if not self.has_mapping or not self.mapping_concerns():
            return None
        return self[ACKNOWLEDGE_FIELD]

    # -- validation ----------------------------------------------------------

    def clean(self):
        cleaned = super().clean()
        if not self.has_mapping:
            return cleaned

        self._refuse_stranding_the_mapping(cleaned)

        posted = {}
        for slot in params.SLOTS:
            name = field_name(slot.key)
            variable = cleaned.get(name)
            if variable is not None and not self._slot_accepts(slot.key, variable, name):
                variable = None
            posted[slot.key] = variable

        self._concerns = mapping.concerns(posted)
        if self._concerns and not cleaned.get(ACKNOWLEDGE_FIELD):
            self.add_error(ACKNOWLEDGE_FIELD, ACKNOWLEDGEMENT_REQUIRED)

        return cleaned

    def _slot_accepts(self, slot_key: str, variable, name: str) -> bool:
        """Whether the model would take this variable in this slot.

        Asked *of the model*, by building the row and cleaning it, rather than
        by repeating its two conditions here. There is one unit refusal in this
        plugin and one sentence explaining it, and a form that composed its own
        would be the second — free to drift from the one the shell, the planner
        and any later API all still read.
        """
        row = FortiVariableMapping(publication=self.instance, slot=slot_key, variable=variable)
        try:
            row.clean()
        except ValidationError as refusal:
            self.add_error(name, refusal.messages)
            return False
        return True

    def _refuse_stranding_the_mapping(self, cleaned) -> None:
        """A filled mapping does not follow its publication to another collection.

        The rows name variables of the collection they were filled from, and the
        model refuses a variable belonging to another — so a re-pointed
        publication holds eight rows that cannot be saved and cannot publish. It
        was reachable before this form existed and invisible until the next run;
        it is refused here because this is the first surface that has both facts
        in hand at once.

        Only when something is actually filled. A publication configured ahead
        of its data, pointed at the wrong collection, is an ordinary mistake with
        nothing yet to strand.
        """
        collection = cleaned.get("collection")
        if collection is None or collection.pk == self.instance.collection_id:
            return

        filled = self.instance.variable_mappings.exclude(variable=None).count()
        if filled:
            self.add_error(
                "collection",
                COLLECTION_IS_MAPPED.format(
                    filled=filled,
                    total=len(params.SLOT_KEYS),
                    collection=self.instance.collection.slug,
                ),
            )

    # -- writing -------------------------------------------------------------

    def save(self, commit=True):
        """Save the publication, then its mapping.

        In that order because a row is written through
        :meth:`~.models.FortiVariableMapping.save`, which asks the publication to
        raise its generation when the variable moved — and the publication has to
        be the saved one for that to land.

        All eight rows are written at every submit and the model decides which of
        them changed. It reads what the row held from the database rather than
        from memory, so it is the only thing here in a position to know; a second
        comparison in this method would be the same rule in a second place,
        differing from the first exactly when a concurrent writer made it matter.
        """
        instance = super().save(commit)
        if commit and self.has_mapping:
            self._save_mapping()
        return instance

    def _save_mapping(self) -> None:
        rows = {row.slot: row for row in self.instance.variable_mappings.all()}
        for slot in params.SLOTS:
            row = rows.get(slot.key) or FortiVariableMapping(publication=self.instance, slot=slot.key)
            row.variable = self.cleaned_data.get(field_name(slot.key))
            row.save()


def _mapping_fields() -> dict:
    """The eight choosers and the tick-box, keyed by field name.

    Built from :data:`~.parameters.SLOTS` so that a parameter added with a new
    source becomes an editable slot in the same commit that adds it — the same
    reason the vocabulary itself is derived rather than typed out.

    The queryset is ``none()`` here and narrowed per form: the class is built
    once for the life of the process and a queryset on it would be one
    publication's collection serving every publication after it.
    """
    fields = {
        field_name(slot.key): SlotChoiceField(
            queryset=Variable.objects.none(),
            required=False,
            label=slot.key,
            empty_label=EMPTY_LABEL,
        )
        for slot in params.SLOTS
    }
    fields[ACKNOWLEDGE_FIELD] = forms.BooleanField(required=False, label=ACKNOWLEDGEMENT_LABEL)
    return fields


#: Assembled rather than written as a class body, because the fields are the
#: vocabulary and the vocabulary is derived. The metaclass collects them from the
#: namespace exactly as it would from a class body, so what comes out is an
#: ordinary ``WagtailAdminModelForm`` subclass.
FortiPublicationForm = type(
    "FortiPublicationForm",
    (VariableMappingMixin, WagtailAdminModelForm),
    _mapping_fields(),
)
