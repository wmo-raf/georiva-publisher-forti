# The published version learns about configuration

## Status

accepted

**Supersedes D8** of the Forti point-plane plan
(`georiva-project/design/forti-implementation-plan.md`), which is struck there
and replaced by **D25**. D8 is not withdrawn for being wrong — every property it
established is preserved here, and one of them is the reason this change is safe
at all. It is superseded because the thing it identified turned out to be
narrower than the thing that needed identifying.

Implements #9. Changes `planner.py`, `models.py` and `publisher.py`; nothing
outside the plugin, and `RunIngestion.version` in core is untouched.

## Context

D8 made the published version `ref_epoch_seconds × 100 + revision`, and gave the
reason: `rawdataforecaster` reloads only on a **strictly greater** version
(`forecast.go:293`), so a bare reference time means a corrected republish of one
run reuses its integer and is ignored forever by every already-running instance
while a restarting one picks it up. Two instances, same version, different data,
no error anywhere.

That stamp answers **"which run is this?"**. What the reader needs answered is
**"are these the same bytes I already have?"** — and the two are the same
question only for as long as the run is the only thing that can change the bytes.

It is not. A publication carries configuration of its own: its extent, its
parameter mapping, its `time_until_next_hours`, and whatever else the chooser and
the editable variable mapping of #7 will add. Change one of those and republish,
and the publisher writes **correct, different bytes for the same run under an
unchanged stamp**. The reader compares, finds nothing greater, and serves the old
data indefinitely.

The failure mode is the one this project keeps meeting: **silent, and silent in
the direction that looks like success.** The verification panel (D23) compares
four hops by sha and reports `published = available = loaded`. All three would be
equal. They would be equal because all three are the *old* version — agreement
reported over a real disagreement, by the instrument built to catch exactly this.

This was found before anything could trigger it. Nothing in the plugin bumps a
generation automatically today, and no configuration change reaches the bytes
yet. That is the point: the stamp has to be able to express the difference before
the first thing that makes one is built on top of it.

## Decision

**The publication's version is `run.version × 100 + generation`.**

Expanded, `ref_epoch_seconds × 10 000 + revision × 100 + generation`: three terms,
ordered **model time → run republish → configuration generation**. The generation
counts changes to the published bytes that are not changes to the run, and lives
on `FortiPublication` as an operator-editable field.

### Model time still leads, which is what makes this safe

D8's load-bearing property was not the revision. It was the *ordering*: keying on
the reference time first means a backfilled older run can never outrank a newer
one. Multiplying the whole run version by 100 and adding below it preserves that
exactly — every term the run supplies keeps its relative weight, and the new one
sits strictly beneath both. A new run at generation 0 outranks any generation of
the run before it, and a reopened run at generation 0 outranks any generation of
its own earlier revision.

So this widens what the stamp *identifies* without moving what it *orders by*.

### The generation resets per run

Because model time dominates, a generation carried across runs would count
nothing: a new run already outranks. Carrying it would also make it a running
total that eventually hits the ceiling for no reason. It resets whenever the run
being published is not the one it was last raised against.

Which run that was is read back out of `published_version` by integer division,
not stored in a column beside it. A second column would be a second authority on
one fact, and the two would part company — most plausibly when a publish died
between the two writes, which is precisely the moment an operator is reading them
to work out what happened.

`FortiPublication.stored_generation_for(run_version)` owns both the division and
the reset, and reads the row from the **database** rather than from the instance
in hand. That is not fastidiousness: `_published_under_another_slug` already
documents, on this same field, that "the build discipline writes every transition
with a queryset `update()`, so an in-memory instance's idea of
`published_version` is routinely stale by exactly the transition that matters". A
stale caller here would see no publish, reset to 0, and move the pointer
*backwards* — the failure this mechanism exists to prevent, arrived at from the
inside. A planner test holds an instance across a publish and asserts it.

### The ceiling is refused, not wrapped

`GENERATION_CEILING = 100`, and publishing there raises `PublicationRefused`.

100 is not a bigger number than 99 in this scheme. The generation occupies the
same two digits the revision shifts into, so `run.version × 100 + 100` is
**exactly** `(run.version + 1) × 100 + 0` — the stamp this run claims at its next
revision, generation 0. Two different sets of bytes would claim one integer, and
a collision by *equality* is the one failure strictly-greater cannot detect: the
reader reads the second as one it already holds. A test asserts the two
expressions are equal, so the refusal cannot quietly stop being necessary.

Refusing is affordable because the generation is not a scarce resource: the next
run resets it, and 99 hand-republishes of a single run is not an operational
pattern — it is a sign the run should not be published at all.

### The fingerprint learns about it too

`PublishPlan.fingerprint` covered the run version, the step count, the first and
last valid time and the parameter names. A configuration change moves none of
them. Without the generation in the material, `publish()` returns early on
`is_up_to_date` — which knows nothing about the generation — and the bump that
was the whole point is never written.

This is the same trap the slug rename hit in the cutover (ADR 0002's companion
work, `rename_forti_model`), where clearing `published_version` without clearing
`input_fingerprint` produced a cutover that silently stopped half way. Same
shape, same fix: the skip check has to be able to see everything that changes the
bytes.

The material now carries the run version and the generation as separate entries
rather than the combined stamp. Equivalent as a hash — the stamp determines both
— but it reads as what it is, and it does not quietly depend on the arithmetic
staying invertible.

## Consequences

**One republish on upgrade, and the pointer only moves forwards.** Rows published
by an earlier version of this plugin hold a *run* version in
`published_version`. `published_run_version` divides that by 100 and gets roughly
the reference time's epoch seconds — a number no live run's version equals — so
the generation resets to 0 on the first publish after the upgrade. That is the
right answer for an unknown predecessor, and the new stamp is ~100× the old one,
so no pointer goes backwards and no reader refuses a load. No data migration is
needed and none is written; migration `0004` adds a column and corrects a help
text.

**The stamp is now 14 digits, and every consumer of it is 64-bit.** Checked
rather than assumed, because a 32-bit reader would have truncated silently and
the symptom — a reader that will not load a new version — is the same symptom
this change exists to remove. `forti-internalformat/structs.go:13` declares
`Version int`; `client.go:99` parses the pointer with
`fmt.Fscanf(r, "%d", &version)` into `var version int`; and Go's `int` is 64 bits
on every architecture the pair is built for — the images running on the dev
instance, `forti-rawdataforecaster:v0.8.1-wmo.1` and
`forti-jsonfrontend:v0.8.1-wmo.1`, both report `arm64/linux`. The value also
stays under 2^53, so the JSON number in `complete.json` and `meta.json` survives
any float64 decoder. Headroom to the year 2100 is ~4.1 × 10^13 against a 2^63
bound.

**Not yet observed against the running pair.** The last of #9's criteria — the
greater stamp in the pointer, the second version directory, and the resident
reader loading it without a restart — are covered at the publisher seam against a
real filesystem-backed sink, but not on the dev instance: every `RunIngestion` of
`ecmwf-ifs-surface` currently has **zero** COG assets, so `plan()` refuses with
`no timestep has a COG for every variable` and no publish is possible there at
all, with or without this change. The first ingested run demonstrates it, and it
is worth watching for one thing specifically: the live row still holds an
old-scale `published_version` (`178925760000`), so that publish is the upgrade
path above. Its pointer should land on `17892576000000` — a hundredfold jump, the
generation reset to 0, and the reader loading it within 3 s.

**The generation is the only configuration the fingerprint sees.** It is a
manual proxy for "something about this publication changed", not a derived hash
of the publication's fields, so editing `time_until_next_hours` and republishing
still skips as up to date. The extent is the one configuration field already
guarded independently — a changed bbox moves the point list, and `GridMoved`
refuses the publish outright rather than skipping it. Making a configuration edit
bump the generation by itself is #7's, alongside the chooser and the editable
variable mapping that define which edits count; doing it here would have meant
guessing that list before it exists.

**Raising the generation does not by itself schedule a publish.** The field is
configuration; `mark_stale()` is what makes the sweep pick a row up, exactly as
for the bbox and every other configuration field on this model. The README
documents the two-step. Wiring a configuration change to *both* is #7's business,
and doing it here would have meant guessing which changes count before the
chooser exists to define them.

**`publish()` writes the generation back through `mark_ready`.** The planner may
have reset it, and a row reading 4 while its stamp ends `00` is a row whose next
hand-raise to 5 names bytes that were never written. The plan is the authority:
it is the value the stamp on the bucket was built from.

**What is still not expressible.** A configuration change made *between* two
runs, before the new run publishes, is absorbed silently — the new run resets to
0 and republishes everything anyway, so the bytes are right, but the generation
records nothing about why. That is correct behaviour and not a lost signal: the
build log carries the version, and the run change is the reason.
