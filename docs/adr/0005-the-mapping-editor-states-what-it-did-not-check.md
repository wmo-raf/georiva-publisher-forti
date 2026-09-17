# The mapping editor states what it did not check

## Status

accepted

Implements #13, under #7. Adds `forms.py`, one panel template, and
`mapping.concerns`; changes `parameters.py` and `wagtail_hooks.py`. No model
change and no migration — the mapping has been data since
[ADR 0004](0004-the-variable-mapping-is-data.md), and this is only its surface.

Closes the thread ADR 0004 left open in its last paragraph:

> No machine check catches the confusion that matters. … #13 is where the
> surface has to say so, because an operator who reads a saved mapping as a
> verified one has been misled by the surface, and that is worse than not
> warning at all.

## Context

The eight slots were editable only from a shell. The README carried the snippet,
which is a fair way to configure one live publication and no way at all to
configure an NMHS's, and the refusal an operator most needs to meet — the unit
check — could only be met by remembering to call `full_clean()` before `save()`.

The feature is not, however, a form over eight foreign keys. It is a surface that
has to distinguish **three** answers where a form ordinarily offers two, and the
third is the one that carries the feature:

- a **refusal**, for a unit that disagrees with the slot;
- a **warning**, for a mapping that is legal and also what a mistake looks like;
- an **admission**, for everything neither of those sees.

The admission is not a caveat on the feature. It is the feature's main claim
about itself. Dew point mapped into the air-temperature slot is degrees celsius
into degrees celsius: the unit check passes, both warnings stay silent, the COGs
are read, the bytes are written, `rawdataforecaster` serves them, and a consumer
reads a dew point labelled `air_temperature`. Every layer agrees. A page that
rendered two green checks and saved would have told an operator that this had
been looked at.

## Decision

### Eight fields on the publication form, not an inline panel

The slot set is fixed and derived from the parameter map. An `InlinePanel` would
offer an add button and a delete button over a table where neither operation has
a meaning — a ninth row is a slot the format does not have, and a deleted row is
a slot that comes back blank at the next seed. The panel renders
`parameters.SLOTS` and nothing else.

The fields are **built** from that vocabulary rather than written into a class
body, for the reason `parameters.py` derives the vocabulary in the first place: a
parameter added with a new `source` becomes an editable slot in the same commit,
instead of a source with no chooser. A slot key is also not a Python identifier —
`2t` — so `forms.field_name` has to exist regardless.

### The mapping is absent from the add form

A slot chooses between the variables of *this publication's* collection, and on
the add form there is no collection yet. Rendering eight empty choosers would ask
for eight decisions that cannot be made and would then be seeded over.

What happens instead is what already happened: `seed_mapping` fills all eight
rows by slug auto-match at creation. The common case therefore still asks for no
decisions at all, and the uncommon one is answered on the page that knows which
collection it is asking about.

### The unit refusal is the model's, asked through the form

`forms` builds the candidate `FortiVariableMapping` and calls its `clean()`,
rather than composing its own message. There is one unit refusal in this plugin
and one sentence explaining it — naming the parameters that would have carried
the wrong number — and a form that wrote a second would be free to drift from the
one the shell, the planner and any later API still read.

### Two warnings, and what makes each weak

**A variable that fills two slots.** The message is symmetric and appears on both
rows: the concern is not that this slot is shared but that these slots are one
series, and a reader of either row should not have to find the other.

**A declared range that cannot reach the slot's.** `parameters.PLAUSIBLE_RANGES`
gives each slot the span its values occupy, declared for all eight and checked at
import for the same reason the expected unit is: a slot silently missing one is
not loudly unchecked, it is quietly unsuspectable.

The comparison is **overlap, not containment**, and that is the whole of what
makes it worth reading. Core documents `value_min`/`value_max` as a styling hint —
"used for color mapping, COG encoding range, and legend display" — and seeds 0–1
on any variable nobody has styled (core's ADR 0022), which is most of them.
Warning on a generous range, or on the untuned default, would produce a warning
on almost every publication and would therefore be dismissed on every
publication. A range sharing *no value* with the slot's is a different claim:
whatever this variable holds, the slot cannot publish it. What it catches in
practice is a variable holding kelvin numbers under a `degC` unit row — the one
mistake shaped like the unit check that the unit check cannot see, because the
unit check reads the label.

### The acknowledgement is per submit and is not stored

A tick-box, shown only when something was raised, required when it is. Not a
model field, because what is acknowledged is *those* warnings at *that* submit: a
stored flag would go on claiming an acknowledgement after the mapping it was
given for had been edited away, which is the failure mode of every "I have read
the above" that persists.

Shown only when raised, because a box present at every save is a box ticked
without being read, and the whole value of this one is that it is rare.

This is narrower than #7's story 14, which asks for a deliberate mapping to be
"recorded as deliberate". Recording it means a model field, a migration, and a
decision about what invalidates the record — all of which #13 does not ask for.
The prompt is implemented; the record is not, and is a ticket of its own if the
audit trail is wanted.

### The admission is rendered, not merely true

`forms.CHECKS_MADE` and `forms.CHECKS_NOT_MADE` are data, rendered by the panel
and asserted by name in `tests/test_admin.py`. They are the one thing on this page
that would go unnoticed if it quietly stopped rendering: the checks would still
work and the page would still look right, which is exactly the state this ADR
exists to prevent.

They name the confusion rather than gesturing at it. "Some checks are not made"
is true and useless; "dew point mapped into the air-temperature slot is degrees
celsius into degrees celsius" is what an operator can act on.

### A filled mapping does not follow its publication to another collection

`FortiPublication.collection` was editable and nothing stopped a re-point. The
eight rows then name variables of the old collection: `FortiVariableMapping.clean`
refuses each one, so the mapping cannot be saved; nothing revalidates them, so
nothing says so; and the publish fails at the next run as a collection with no
assets. Reachable before this editor existed and invisible until then.

Refused on the form, and only when something is actually filled — a publication
configured ahead of its data and pointed at the wrong collection is an ordinary
mistake with nothing yet to strand. On the form rather than the model because the
form is what has both facts in hand at once, and because the shell path that would
otherwise need it is the same one that is already expected to call `full_clean()`.

### All eight rows are written at every submit

`FortiVariableMapping.save` already reads what the row held **from the database**
and raises the generation only when the variable moved. The form writes all eight
and lets it decide, rather than comparing again here: two comparisons of the same
thing differ exactly when a concurrent writer makes it matter, and the model's is
the one in a position to know. ADR 0004 anticipated this submit shape and is the
reason that guard exists at all.

## Consequences

**The generation is spent on changes, not on submits.** A submit that changed no
mapping raises nothing and leaves a READY publication READY; a submit that
remapped one slot raises it by one and marks the row stale. Asserted at the admin
seam rather than at the model, because the eight-rows-per-submit shape is the
editor's and the ceiling it guards is two digits wide.

**The warnings are not comprehensive and the page says so.** Both are properties
of what the collection *declares* about itself. Nothing on this page reads a
raster, and a check that did — sampling a COG and asking whether the numbers look
like a temperature — would be a different feature with a different deadline
behind an edit form.

**A collection re-point now needs the mapping cleared first.** New refusal, on a
path that previously succeeded and produced an unpublishable publication. It is a
behaviour change and not only a surface one, which is why it is here rather than
in a commit message.

**Readiness is still not on this page.** Which slots resolve against the selected
collection, how many closed runs there are, and how many steps the latest run
would publish are #14's, beside the form rather than inside the mapping section.
This ticket renders what the mapping *is*; that one renders what it would *do*.
