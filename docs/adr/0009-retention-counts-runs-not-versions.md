# Retention counts runs, not versions

## Status

accepted

Settles the retention question #7 left open in its further notes. Changes
`publisher.prune` and renames `VERSIONS_KEPT` to `RUNS_KEPT`. No model change and
no migration.

Follows from [ADR 0003](0003-the-published-version-learns-about-configuration.md),
whose generation is what made the old count wrong.

## Context

Retention kept the newest **5 versions** per area. Under D8 a version was a run,
or a republish of one, so 5 versions meant 5 runs to point a reader back through.

ADR 0003 made a version `run.version × 100 + generation`, and a remap now
publishes a new version of the *same* run. Each one spent a retention slot. Four
remaps of one run filled all five and pruned the run before it.

That is the wrong thing to lose. There are two ways a publish goes bad, and each
rolls back to something different:

- **A bad run** rolls back to the previous run. Every generation of the bad run
  carries the same bad data, so none of them is a way out.
- **A bad remap** rolls back to the previous generation of the same run.

Counting versions could lose the first. Keeping only the newest generation of
each run would lose the second.

## Decision

**Retention keeps every version belonging to the newest 5 runs**, where a run is
`version // GENERATIONS_PER_REVISION` — reference time and revision, exactly what
D8 counted. Generations ride with their run: they never spend a slot, and when a
run falls out of retention all its generations go with it.

The version `latest/<area>` names is still never a candidate, whatever run it
belongs to.

## Consequences

**"5" means what it meant under D8.** An operator reading the retention setting
gets the number of runs they can roll back through, regardless of how often the
mapping was corrected.

**Storage is bounded per run by the generation ceiling**, which ADR 0003 sets at
100 — about 300 MB per run at ~3 MB a version. The per-run reset makes that
unreachable in practice; a run remapped a handful of times costs a handful of
versions.

**A revision is a run here**, as it was under D8. A reopened run spends a slot,
because its bytes come from different data and it is a legitimate rollback
target in its own right.
