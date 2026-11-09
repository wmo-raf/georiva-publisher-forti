# Configuring and operating publications

The publication form, the variable mapping, readiness, the tasks, and the two operations that need care: republishing and renaming.

## Configuration

One publication per collection, in the Wagtail admin under **Forti publications**:

- **Collection** — must be a **forecast** collection, and the chooser offers no
  others. Only a forecast collection has run boundaries; nothing opens a
  `RunIngestion` for any other kind, so a publication over one would wait forever
  for a run that never comes. The rule is in `clean()` as well as in the chooser,
  so a posted id meets it too. An `internal` collection is refused for a
  different reason: it is a derivation intermediate, not a dataset. A `private`
  collection saves and does **not** publish: the serving plane can narrow a
  private model to its organisation (D18), but `planner.plan` refuses any
  collection that is not `public` outright, on the ground that the reader itself
  holds no credential. Readiness reports that refusal on the form rather than
  leaving it to the first build.
- **Area** — the name readers ask for, unique within the organisation. A
  *missing* `latest/<area>` reads as version **0** and sends the reader looking
  for `<area>/0/complete.json`: fatal at startup, and the error does not mention
  the area name.
- **Extent** — give it a margin. A border town is asked for from both sides, and
  an area that stops at the boundary answers "outside coverage" to half of them.
  The resulting point list is **pinned**: a later build that produces a different
  one is refused rather than published, because every stored value is addressed
  by an ordinal into that list and `rawdataforecaster` caches its s2 index under
  the list's MD5.

## The variable mapping

Eight of the fifteen parameters are derived after the transpose and read no
variable of their own. The other seven, plus `tp` — which no parameter publishes
directly and four derivations read — are the **eight slots** a publication maps.
The set is fixed, derived from the parameter map in `parameters.py`, and is not a
list an operator adds to: what is editable is the variable in each row.

```bash
georiva shell -c "
from georiva_publisher_forti.models import FortiPublication
p = FortiPublication.objects.get(slug='ecmwf-ifs')
for slot, variable in p.mapped_variables().items():
    print(slot, '->', variable.slug if variable else '(blank)')
print('not ready:', p.unmapped_slots())
"
```

A new publication is seeded by **slug auto-match** — the rule the planner used
before the mapping existed — so a collection using the conventional names needs
no configuration at all, and editing is purely an override. A collection that
names its variables otherwise gets blank rows to fill in rather than a refusal it
can do nothing about.

A **blank slot** saves and is reported by the Readiness section above the
mapping; the publish is refused by slot name. A slot whose variable carries the
**wrong unit** is refused at both the mapping and the planner. Neither refusal
can catch the confusion that matters: dew point mapped into the air-temperature
slot is celsius into celsius, and every layer downstream agrees.

### Editing it

**Publications → Forti → *one model* → Mapping**: a page of its own, reached from
the listing row and from the inspect page, rendering the eight slots and only
those. It is not on the publication form, where the collection is still being
chosen and there are therefore no variables to choose between; creation seeds
all eight rows by auto-match and lands the operator on this page, so that what
auto-match found is read by the one person who can judge it. Saving returns to
the inspect page, whose readiness is the answer to "is it ready now?".

Three answers, not two:

| | |
|---|---|
| A unit that disagrees with the slot | **refused**, naming the parameters that would have carried the wrong number |
| A variable that fills two slots | **warns**; saves once acknowledged |
| A declared range that cannot reach the slot's | **warns**; saves once acknowledged |
| A blank slot | saves, shown as `no variable yet` |
| Everything else | **not checked**, and the page says so |

The acknowledgement is a tick-box that appears when something was raised **and this
submit moves a slot**, and it is not stored: what is acknowledged is *those* warnings
at *that* submit. A publication already living with a warning is not asked again
when its mapping is re-saved unchanged — the warning is still shown, it just does
not stop a submit nobody made it with.

The warnings are drawn by `mapping.concerns()` and are deliberately weak. The range
check compares the variable's declared range — which core documents as a styling
hint — against `parameters.PLAUSIBLE_RANGES`, and fires only when the two share no
value at all; a generous range, or core's untuned 0–1 default, says nothing. What
it catches is a variable holding another unit's numbers under the right unit label,
which the unit check cannot see because the unit check reads the label.

**No machine check catches the confusion that matters**, and the form says so on
the page rather than only in this file. An operator who reads a saved mapping as a
verified one has been misled by the surface.

A **filled** mapping cannot follow its publication to another collection: the rows
name variables of the collection they were filled from, so the publication form
refuses the change. Clear the mapping first, or make a second publication.

From a shell, equivalently:

```bash
georiva shell -c "
from georiva_publisher_forti.models import FortiPublication
p = FortiPublication.objects.get(slug='ecmwf-ifs')
row = p.variable_mappings.get(slot='2t')
row.variable = p.collection.variables.get(slug='temperature-2m')
row.full_clean()   # the unit check, before the write rather than after
row.save()
"
```

A publication created before its collection declares its variables is seeded with
eight blank rows, and they are **not** re-matched silently — auto-match runs once, at
creation, because re-running it would undo a deliberate edit. Once the variables
arrive, the mapping page **offers** the match instead: it names the blank rows a
same-named variable could fill, and *Fill these rows and save* applies it. That is
an ordinary save — the unit refusal, the warnings and the acknowledgement all
apply, and the generation rises once per row it filled. A filled row is never
offered, whatever its slug; neither is a match in the wrong unit, which the save
would only refuse; and the offer is absent when it would fill nothing.

Changing a mapping **raises the generation and marks the publication stale**, so
the next publish outranks the last and the sweep picks it up within five minutes
rather than at the next run. Saving a row that did not change counts for
nothing. An edit made while a build is already in flight is the exception — that
build finishes under the old mapping, and the correction lands at the next run
instead. Deleting a variable a publication maps is refused
by the database; `variable.forti_slots` answers which publications read it.

See `adr/0004-the-variable-mapping-is-data.md` and
`adr/0005-the-mapping-editor-states-what-it-did-not-check.md`.

## Readiness

**Publications → Forti → *one model* → Inspect** carries a **Readiness** section
above the mapping: what a publish would meet, read from the database before
anything is written. Every fact in it was discoverable before only by
publishing — one refusal at a time, into a build log nothing rendered.

Six findings, in the order an operator meets them:

| finding | what it answers |
|---|---|
| Forecast collection | whether the collection is one at all — the chooser guards this, and a collection can be changed under a publication afterwards |
| Collection visibility | whether a reader presenting no credential may read it; the build refuses anything but `public` |
| Closed runs | how many have closed and when the latest was |
| Slot coverage | whether every slot names a variable of this collection |
| Units | whether each filled slot carries the unit its parameter publishes |
| Steps the latest run would publish | the size of the step intersection, counted through the planner's own two functions |

Each carries one of five states, and **the distinction that matters is between
two of them**:

- **not ready yet** — nothing is misconfigured and waiting is the correct action.
  A collection set up the afternoon before its first ingestion is here;
- **needs a change** — waiting fixes nothing, and every hour spent waiting is an
  hour the model is not served. A blank slot is here;
- **still ingesting** — it publishes *now*, and would publish more steps once
  the run's stragglers land. The count says `8 of 9` rather than `8`;
- **cannot say yet** — a finding downstream of one of the above, with nothing
  wrong of its own. The step count is this whenever anything above it needs a
  change: a publish is refused before it counts a step, so a number there would
  promise a publish that is not going to happen;
- **ready**.

The whole section's verdict is its **worst** finding's state, derived rather than
stored so that the summary and the rows cannot disagree.

It reads the database and nothing else — no bucket, no status document, no
raster — for the same reason the history panel does: a page behind object
storage's deadline cannot be opened while the bucket is slow.

And it says nothing about whether the bytes would be *right*. Dew point in the
air-temperature slot passes all six findings; what the mapping page did and did
not check is stated on that page, in full.

See `adr/0006-readiness-tells-not-yet-from-never.md`.

## Operations

| task | queue | cadence |
|---|---|---|
| `publish_forti_area` | `georiva-processing` | on a run closing |
| `sweep_forti_publications` | `georiva-default` | 5 minutes |
| `refresh_forti_config` | `georiva-default` | 5 minutes, and inline at the end of a publish |
| `prune_forti_publications` | `georiva-default` | daily |

Nothing goes on `georiva-ingestion`, which runs one pool process and admits fetch
and extraction only (ADR 0025). A publish is derived from data that is already
published and already servable, so nothing a reader can observe waits on it.

Retention keeps **5 runs per area**, every generation of each, count-based, and
never the version `latest/<area>` names however old it is — an area whose feed
has stopped still has a reader following its last good version. A run is
`version // 100` — reference time and revision — so a remap adds a version
without spending a slot: four remaps of one run cannot evict the run before it,
which is what a bad run is rolled back to (ADR 0009).

### Republishing one run after a configuration change

A run's version cannot express "same run, different bytes", so the publication
carries a **generation** beside it. A mapping edit raises it by itself; for any
other configuration change, raise it by hand and republish:

```bash
georiva shell -c "
from georiva_publisher_forti.models import FortiPublication
from georiva_publisher_forti.tasks import dispatch_publish
p = FortiPublication.objects.get(slug='ecmwf-ifs')
p.generation += 1
p.save(update_fields=['generation'])
dispatch_publish(p.pk, force=True)
"
```

The stamp on `latest/<area key>` goes up, `rawdataforecaster` sees a strictly
greater version within 3 s and loads the new bytes without a restart. Leaving the
generation alone and simply republishing does **not** work and does not complain:
the bytes land under an integer the reader already holds, and the verification
panel shows published, available and loaded in agreement over data nobody is
serving.

It resets to 0 on the next run, so it is not a running total — it counts within
one run only. At `generation = 100` the publish is refused rather than wrapped:
that number is the stamp the run's next revision claims.

### Renaming a published model

A model's slug is a segment of every key already on the bucket, so
`FortiPublication` refuses to rename one that has published — from `clean()` and
from `save()` both, which closes the admin form and the shell alike. Two commands
exist for the case where it has to happen anyway. Both preview unless `--apply`.

```bash
georiva rename_forti_model kenya ecmwf-ifs --apply   # 1
#                                                      2. republish
georiva cleanup_forti_orphans --apply                # 3
```

The rename does not step around the guard; it makes the guard's precondition
false, clearing the slug and the whole published state in one `update()`. The row
then says, truthfully, that it has published under no name — so there is no
instant at which the database claims a version under a slug whose bytes are not
on the bucket, and an ordinary `.save()` rename is refused again after the next
publish.

**The order is the whole design.** Deleting the old bytes first leaves the
instance serving nothing for the length of a publish. Deleting after costs a
second pass, because `config.documents()` withholds an empty `areas` list rather
than writing one — so the config on the bucket goes on naming the old area, and
the old bytes go on answering, right up to the moment the new ones land. The
panel shows that interval as hop 1 **withheld**, which is the ordering working
rather than a stale write.

`cleanup_forti_orphans` enforces it: an area key is an orphan only when no row
claims it **and** the bucket's own `rawdataforecaster.json` no longer advertises
it. Run straight after a rename it refuses and says why. It is also the only
thing that removes an area key at all — retention works *within* one, so a
publication deleted by hand leaves its prefix and its `latest/` pointer behind
forever.

With real consumers attached, do none of this: their clients have the old slug
hard-coded and no ordering of ours reaches them. Publish a *second* publication
under the new name, serve both, and retire the old one after a deprecation
window — which is what the model's own refusal message recommends.
