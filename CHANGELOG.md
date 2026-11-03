# Changelog

## 0.1.0

First installable release. Needs GeoRiva core 0.5.0 or later, and the Forti
serving pair from `wmo-raf/forti` at `v0.8.1-wmo.1` (see `deploy/compose.yml`).

### Signed-in callers get a larger rate limit

`GET /api/forecast/` and `GET /api/forecast/{model}/` now throttle anonymous
callers at 60/min (per IP, per organisation) and signed-in callers — a session
or a `grv_…` API key — at 600/min (per user, per organisation). The second tier
is `GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED`; set either to `None` to
turn that tier off. A throttled response is a 429 with `Retry-After`. An NMHS
website should hold a key rather than have the anonymous rate raised.

### The Try it map uses core's basemap

The "Try it" map on the publication inspect page now draws core's shared
basemap — Light, with borders, coastline and labels — in place of an Esri
street-map raster. The style is core's, loaded from its static files, so it
needs the core release that ships `js/georiva-basemap.js` and
`basemap/georiva-basemap.json` (see README, "Which core it needs"). On an older
core the map is not drawn; the rest of the page is unaffected.

### The published version jumps by two orders of magnitude, once

The published version is now `run.version × 100 + generation`
([ADR 0003](docs/adr/0003-the-published-version-learns-about-configuration.md)),
so a configuration change — today, a variable remap — reaches the reader under a
strictly greater stamp instead of being ignored.

**What an operator will see after upgrading:** the first publish of every
publication rewrites its bytes at a new, much larger version — for example from
`178 000 000 000` to `17 800 000 000 000`. **The output is identical; only the
stamp moves.** Each reader reloads once and carries on. This is expected and is
not a sign that anything went wrong.

The step is one-way and safe: every new-scheme stamp exceeds every old-scheme
stamp reachable this century, so no older version can ever outrank a newer one,
and no published bytes need migrating. Stamps stay below JavaScript's
safe-integer limit through 2100.

### Retention counts runs, not versions

Retention keeps the newest **5 runs** per area, with every generation of each,
instead of the newest 5 versions
([ADR 0009](docs/adr/0009-retention-counts-runs-not-versions.md)). Remapping a
run no longer spends a retention slot, so repeated remaps cannot evict the
previous run — the one a bad run is rolled back to. The version `latest/<area>`
names is still never pruned.

### Also in this release

- The collection chooser offers only forecast collections, and the model
  refuses any other (#8).
- An editable mapping of the eight source slots to the collection's variables,
  pre-filled by slug, with a hard unit check and acknowledged warnings
  (#12, #13, #22).
- Readiness beside the publication form, telling "not ready yet" from "will
  never work" (#14).
- The publications index shows the version each reader actually has resident,
  and the detail page shows the publish and retention history (#10, #11).
