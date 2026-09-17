# The variable mapping is data, and a slot is not a slug

## Status

accepted

Implements #12, under #7. Changes `parameters.py`, `planner.py`, `models.py` and
`publisher.py`, and adds `mapping.py`, `units.py` and one migration. Nothing
outside the plugin; core is untouched.

Closes one thread left open by [ADR 0003](0003-the-published-version-learns-about-configuration.md),
which introduced the generation and said that making a configuration change bump
it "is #7's, alongside the chooser and the editable variable mapping that define
which edits count". This is the first such edit, and it bumps.

## Context

Which GeoRiva variable fed which Forti parameter was an exact slug match inside
the planner: `collection.variables.filter(slug__in=needed)`, against a set of
slugs typed into the parameter map. A collection whose 2-metre temperature was
called `temperature-2m` rather than `2t` could not publish at all, and the only
way to find that out was to configure a publication, wait for a run to close,
and read `is missing variable(s) 2t` out of a build log nothing renders.

The names in the parameter map are ECMWF's GRIB shortNames. They are the right
default — every collection this plugin has published uses them, because the IFS
source plugin provisions them — and they are not a rule anyone agreed to. An
NMHS naming its variables after what its forecasters call them is not
misconfigured.

The fix is not to widen the match. Fuzzy matching on a value whose unit is
copied out of `meta.json` without interpretation is the worst possible place to
guess: a wrong guess publishes a plausible number under a right-looking label,
and every layer downstream agrees.

## Decision

**The mapping is one row per slot, per publication, with a foreign key to the
variable.**

### A slot is a role, and the vocabulary is derived from the parameter map

Of the fifteen Forti parameters, seven read a variable directly and eight are
derived after the transpose — relative humidity from two sources, the weather
symbol, the six- and twelve-hour windows, the max and the min. A derived
parameter has no source variable and nothing to map. Adding `tp`, which no
parameter publishes directly and four derivations read, the mappable set is
**eight**.

`parameters.SLOTS` is built from the table rather than typed out beside it, so a
parameter added with a new `source` becomes a slot in the same commit instead of
a source nothing can be mapped to. Each slot carries the unit it expects and the
parameters that read it, both derived the same way.

A slot is *named by* the conventional slug — `2t`, `tp` — which keeps the
default legible and lets `publisher.py` go on saying `cubes["2t"]`. But the key
is now a slot key, not a variable slug, and the two only coincide by default.

### Seeded by the rule it replaces

`mapping.auto_match` is the old exact-slug match, kept as the default and applied
at two moments: the migration that describes what the live publication is already
serving, and the creation of any new publication. So a conventionally named
collection resolves to exactly what it resolved to before, with nobody
configuring anything, and editing is purely an override.

It is a pure function in a module of its own because a data migration imports it.
A migration that reached into `models` would rewrite its own history the next
time a model changed; one that copied the rule would describe the bytes a
publication is serving using a rule that is no longer the one that produced them.

### A blank slot is legal, and unpublishable

Saveable, so a publication can be configured while its collection is still
declaring its variables, and refused at the planner by name. That replaces the
old "is missing variable(s) 2t" — same refusal, one step closer to the setting an
operator can actually change, and now discoverable before a run closes rather
than after one.

### The unit check stays a hard refusal, and is now made twice

At the mapping, where a slot is given a variable, and at the planner, just before
anything is read. `units.same_unit` is the one comparison, extracted from the
planner so the two seams cannot part company: compared through pint, so `°C` and
`degC` agree and `K` does not — compatible is not the same unit, and Forti
converts nothing.

Twice rather than once because they answer different questions. The model's
`clean()` is what a form can show; the planner's is what holds when nothing went
through a form, which includes the shell, the migration and any later API.

### Changing a mapping raises the generation and marks the row stale

Both, and neither is sufficient. The generation is what makes the next publish
outrank the last — `rawdataforecaster` reloads only on a strictly greater
version, and a remap moves nothing the run knows about, so without it the
corrected bytes land under the integer the reader already holds and are never
loaded while every surface reports agreement. `mark_stale` is what makes there
*be* a next publish before the next run closes, which for a twice-daily model is
up to twelve hours away.

Seeding deliberately bypasses both, through `bulk_create`: a publication being
born is not its bytes changing, and a first run published at generation 8 would
carry a stamp claiming to be a correction of itself.

### The fingerprint covers the mapping

The same run read through a different variable is different bytes. The generation
would catch a remap on its own — the same edit raises it — but a fingerprint that
did not name the inputs it actually read would call two different reads
identical, which is precisely what that value is for.

### `RESTRICT`, not `PROTECT`

Both refuse to let a mapped variable be deleted out from under a live
publication, which is the point: a dangling mapping would otherwise be
discovered at the next publish, by which time the operator who deleted the
variable is somewhere else.

`PROTECT` refuses *unconditionally*. A collection cascades to its variables and
to its publication both, so with `PROTECT` deleting a published collection raises
`ProtectedError` over rows that were themselves about to be deleted — measured,
not reasoned: the test that deletes a collection fails with
`Cannot delete some instances of model 'Collection' … 'Variable.collection'`
listing all eight mapping rows. `RESTRICT` admits exactly that case — the mapping
may go if it goes with the publication that owns it — and refuses the one that
matters.

The foreign key is also what makes "which publications read this variable"
answerable by query, which is the question asked immediately before every rename
and every deletion. A JSON blob on the publication could do neither.

## Consequences

**Verified end to end on the dev instance.** Dew point remapped to the
temperature variable by hand, published, and `GET /api/forecast/ecmwf-ifs/`
returned `dew_point_temperature = 20.8` against `air_temperature = 20.8` where it
had read `10.7`; remapped back, republished, and it read `10.7` again. The
pointer moved to `17895168000001` and the reader loaded it without a restart,
with `published = available = loaded` afterwards. No user interface was involved,
which was the criterion.

**The live publication's first publish after this jumped a hundredfold**, from
`178951680000` to `17895168000000` — the upgrade path ADR 0003 predicted for a
row still holding an old-scale stamp, observed here rather than at the first
ingested run.

**An edit made while a build is in flight is published at the next run**, not
within five minutes. `mark_stale` is a no-op on a BUILDING row, deliberately:
overwriting it would drop a live claim. The build in flight finishes under the
old mapping and marks itself ready, so the correction is delayed, never lost —
the generation is already raised. Closing that window means making `mark_ready`
conditional on the generation the plan was built from, which is a change to
core's build discipline and is not this ticket's.

**The mapping is still the only configuration the fingerprint sees**, beside the
generation. `time_until_next_hours` remains an edit that republishes as up to
date; the extent remains guarded independently by `GridMoved`.

**No machine check catches the confusion that matters.** Dew point mapped into
the air-temperature slot is degrees celsius into degrees celsius, and every layer
downstream agrees. The unit check catches kelvin; nothing catches a plausible
value in the right unit in the wrong slot. #13 is where the surface has to say
so, because an operator who reads a saved mapping as a verified one has been
misled by the surface, and that is worse than not warning at all.
