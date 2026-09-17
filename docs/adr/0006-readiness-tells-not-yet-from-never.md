# Readiness tells "not yet" from "never"

## Status

accepted

Implements #14, under #7. Adds `readiness.py`, one panel template and
`ReadinessPanel`; promotes two of `planner.py`'s private functions to public so
that one implementation answers both callers. No model change and no migration —
every figure was already in the database.

Completes the chooser half of #7, whose problem statement named the fault this
closes:

> Selecting a rainfall collection is accepted, and the mistake surfaces later as
> `NothingToPublish: has no closed run` — a message that blames the run rather
> than the choice, in a build log nothing renders.

## Context

Whether a publication will publish depends on six facts, and an operator could
see none of them from the page they configure it on. Each was discovered at the
first publish, one at a time, in whichever order `planner.plan` happens to check
them, and recorded as a sentence in `FortiPublicationBuildLog` — which #11 now
renders, but only *after* a publish has been attempted.

Two of those facts are fixed by waiting and four are fixed only by an operator,
and the build log says the same kind of thing about both. A publication
configured the afternoon before its first ingestion and a publication with a
blank `2t` slot both read as "the last publish did not work", and the correct
action for the first is to do nothing at all while the correct action for the
second is to do something immediately. Getting that backwards costs either a
pointless investigation or a day of not being served.

That is the whole problem this addresses. The figures are a reading of rows the
plugin already had; the distinction is the feature.

## Decision

### Readiness is a module returning findings

`readiness.report(publication)` returns a frozen `Readiness` holding a tuple of
frozen `Finding`s. The template renders them and decides nothing, which is the
arrangement `verification.py` and `history.py` already use and is the only one in
which the distinctions can be tested without rendering a page — `test_readiness`
asserts eleven of them against the data, and `test_panel` asserts three things
about the page, thinly.

### Five states, and the two that carry the feature

- **waiting** — in the way now, gone by itself later. Wait.
- **blocked** — in the way, and only a change moves it. Act.
- **partial** — publishes now, and would publish more steps later. The count
  reads `8 of 9`.
- **unanswerable** — a question downstream of one of the above, with nothing
  wrong of its own.
- **ready**.

`waiting` and `blocked` are the pair the ticket exists for. `partial` is separate
from both because a run mid-arrival *publishes*, and calling it "not ready yet"
would be a false negative on a publication that is about to serve.

`unanswerable` is separate from `blocked` because the step count of a publication
with a blank slot is not a second fault — it is the same fault seen from
downstream, and two red rows for one cause reads as two things to fix.

The section's verdict is the **worst** finding's state, derived rather than
stored: a summary that could disagree with its own rows is worse than no summary.

### Six findings, not the five the ticket named

The sixth is the collection's **visibility**. `planner._refuse_unpublishable_collection`
refuses any collection that is not `public`, and a readiness section that omitted
it would have reported "ready" about a publication the very next build refuses —
recreating, one refusal further down, exactly the fault this module exists to
repair.

It also surfaces a disagreement worth recording: `FortiPublicationQuerySet.visible_to`
is written to serve a `private` model to its own organisation (D18), and the
planner refuses to build one at all. The serving plane's rule and the build's
rule differ, the README described the former as though it settled the matter, and
nothing said so on any surface. This ADR does not resolve it — the refusal is
tested and unchanged — it only stops it being invisible.

### The step count is the planner's, not a second query shaped like it

`planner._cog_hrefs` and `planner._shared_times` became `cog_hrefs` and
`shared_times`. Readiness calls them; `plan` calls them; there is one answer to
"which COGs does this run have for these slots".

A second implementation would have been four lines and would have been wrong in
the one way that matters: the form would promise a number the build then
disagreed with, and an operator reading both would have no way to tell which had
lied.

### It reads the database and nothing else

No bucket, no status document, no raster — the discipline `PublishHistoryPanel`
already holds this page to. An edit form behind object storage's deadline is one
that cannot be used to correct a bbox while the bucket is slow, and readiness has
no question that needs the serving plane: what is *resident* is the listing's
column and the verification panel's chain, both of which already exist.

### It does not claim the mapping is right

Every finding passes for dew point mapped into the air-temperature slot. That
admission is already made, in full, by the mapping section immediately below
(`forms.CHECKS_NOT_MADE`, [ADR 0005](0005-the-mapping-editor-states-what-it-did-not-check.md)),
and this module does not restate it in different words — two wordings of one
caveat is how one of them comes to be believed to be a different, weaker claim.

## Consequences

**An operator learns at the form what they used to learn from a build.** The
common case — a conventionally named collection with runs closing — reads
`ready` and asks for nothing.

**The page costs five queries**, measured: the collection, the latest closed run,
a count of the closed ones, the mapping rows, and one `Asset` query for the step
intersection — the last of them the planner's own, so it is the query a publish
would make anyway. It runs on every render of every publication edit page, and
none of it leaves the database.

**A publication configured ahead of its data stays selectable and says why.**
That was #7's user story 6 and the reason the chooser guards only `is_forecast`:
everything else is diagnosis, and this is where the diagnosis is now rendered.

**The `private` disagreement is now visible to operators.** A publication over a
private collection reads `needs a change` on a page that used to say nothing —
which is an improvement whichever way the disagreement is eventually settled, and
is a question for a ticket of its own rather than for this one.
