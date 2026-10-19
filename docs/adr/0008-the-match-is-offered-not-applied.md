# The slug match is offered on a blank row, not applied

## Status

accepted

Implements #22, under #7. Adds `mapping.fillable`; changes `forms.py`, the
mapping panel template and `wagtail_hooks.mapping_page`. No model change and no
migration.

Closes the thread [ADR 0005](0005-the-mapping-editor-states-what-it-did-not-check.md)
left in its consequences — "suggesting the match beside a blank slot without
applying it is the shape that would respect both, and is a ticket of its own" —
and leaves [ADR 0004](0004-the-variable-mapping-is-data.md)'s decision standing.

## Context

ADR 0004 runs auto-match once, at creation, and never again: a blank row is an
edit like any other, and re-matching it would undo it at the next save. That is
right, and it left a gap once there was a page. A publication created before its
collection declared its variables keeps eight blank rows; the variables arrive a
week later, conventionally named; and nothing says that the rule the code already
has would now fill all eight. The operator maps them by hand, or does not notice
the publication is unmapped until a publish is refused by slot name.

## Decision

**The page offers the match and the operator applies it.** Never at save, never
silently, and never to a row that is already filled.

`mapping.fillable(mapped, variables)` is the offer: the blank slots
`auto_match` has a variable for, in vocabulary order. Pure, beside the rule it
reuses, and adding no rule of its own. A filled slot is left out whatever its
slug — it is somebody's decision, and the match is a default, not a correction.

The offer is computed from the **stored** mapping, which is what the page was
rendered from, and is shown as one sentence naming every row it would fill, with
a button. It is **absent** when it would fill nothing — when every row is filled,
or when the collection names its variables its own way — because a button that
fills no row reads as one that is broken.

### Applying it is an ordinary save

The button is a second submit of the same form. Taking the offer writes the
offered variables into the *posted data* before anything is cleaned, and from
there the submit is indistinguishable from one where the operator chose each
variable by hand: the unit refusal, the warnings, the acknowledgement and the
model's generation arithmetic all apply unchanged. Nothing new happens on the
write path.

Applying to the data rather than to the cleaned values is what keeps a refusal
honest: the page re-rendered after an acknowledgement is required shows the rows
filled, so what is acknowledged is the mapping on the page, and the plain Save
that follows posts it.

Only rows that were offered **and are still blank in what was posted** are
filled. A row chosen on the page before the button was pressed is the newer
decision.

## Consequences

**The generation rises once per row the offer filled**, through
`FortiVariableMapping.save` as for any remap, and not at all when it filled none.

**An ordinary Save still re-matches nothing.** ADR 0004's guarantee is unchanged
and asserted at the admin seam beside the offer.

**The match is by slug and says nothing about units.** An offered variable in the
wrong unit is refused on applying, like any hand-chosen one; the offer does not
pre-screen it, because a refusal the operator can read is better than a row that
is silently not offered.
