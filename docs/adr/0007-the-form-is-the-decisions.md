# The form is the decisions; the inspect page is the home

## Status

accepted

Splits the publication's one admin page into three — the snippet form, a
mapping page and an inspect page — and gives the plugin its first reachable
menu entry, under a **Publications** group core now owns. Moves
`ReadinessPanel`, `PublishHistoryPanel` and `VariableMappingPanel` off the edit
form: readiness and history to the inspect page, the mapping to a page of its
own. Gives the re-queue its first button. No model change and no migration.

Supersedes the placement decisions of ADR 0005 and ADR 0006 — *where* the
mapping editor and the readiness section sit — and none of their substance.

## Context

By ADR 0006 the edit form carried eight sections, and four of them — readiness,
`status`, `error`, history — were things the publication *reports*. An operator
who opened the page to correct a bbox read a diagnosis first, and an operator
who opened it to read the diagnosis was handed a form. The two are different
acts, and Wagtail draws the line between them itself: an edit view and an
inspect view. The plugin had never enabled the second.

The mapping had the same problem from the other side. It was on the form
because the form is where the publication was configured, but the form is also
where the *collection* is chosen — and the eight slots choose between the
variables of one collection. ADR 0005 resolved that by removing the slots from
the add form and seeding them at creation, which left a gap: after creating a
publication the operator landed on the listing, and nothing told them whether
auto-match had filled eight slots or five.

And none of it was reachable. Core strips Wagtail's Snippets menu, this viewset
registered no menu hook of its own, and the README's *Snippets → Forti
publications* named a path that did not exist.

## Decision

**Three pages, split by act.** The form is the decisions and nothing else: what
is published, its extent, the two advanced fields, and the one mapping rule
only the form can enforce — a filled mapping does not follow its publication
to another collection, because only the form has both facts in hand at once.
The **inspect page** is everything the publication reports, in the order the
questions arrive: what is this, can it publish, what does it publish, what did
it last publish, what has it been doing. It also renders, for the first time,
the fields the form never offered because they are output — `published_version`,
`published_reference_time`, `point_count`, `grid_id`. The **mapping page** is
the eight slots on a page that already knows which collection they are of.

**Readiness on the inspect page only.** The mapping page already renders
slot-level feedback in its rows — `no variable yet`, the concern notes — which
is the two findings that depend on the mapping. The other four are about the
collection and its runs, which that page cannot change. One verdict, on the page
an operator goes to when they ask for one.

**Every save lands where its consequence shows.** Creation lands on the mapping
page, so what auto-match found is read by the one person who can judge it while
the collection is still in mind. An edit and a mapping save both land on the
inspect page. Only a delete returns to the listing.

**The re-queue gets its button, on the inspect page**, beside the status and
history it acts on. It becomes POST-only and gated on the model's `change`
permission, which is what the mapping page and the form ask — as the
virtual-Zarr re-queue in core already is. It was neither: a GET that changed
state, and one a link preview could trigger.

**Core owns the menu group; the plugin registers into it.** Core adds a
**Publications** submenu after Data — catalogued, brought in, sent out — with a
hook, `register_publications_menu_item`, and no children of its own. It hides
itself when nothing has registered. This viewset's `menu_hook` is that hook and
its `menu_label` is *Forti*, under a heading already called Publications; the
page keeps the longer name, which it carries without a parent. The alternative
was `add_to_admin_menu` on the viewset, which is a plugin deciding it deserves
top-level rank in a sidebar it does not own.

## Consequences

The edit form is short again, and the tests that held the panels to
"reads the database and nothing else" now hold the inspect page to it — which
is a stronger claim, because the inspect page is the one an operator opens
*because* something is wrong.

The mapping page is a hand-written view rather than a snippet view, so its
organisation narrowing is `get_org_object_or_404` rather than
`OrgScopedViewSetMixin`. Two mechanisms for one rule, and the tests assert the
404 on both.

`FortiPublicationForm` and `VariableMappingForm` are two classes where there was
one mixin. Everything ADR 0005 decided about the second — the three answers, the
acknowledgement that is not stored, what the page admits — is unchanged; only
its `instance` became a `publication` it reads rather than saves.

A second publisher plugin, if one arrives, registers a sibling of *Forti* in the
same group and inherits nothing else from this one.
