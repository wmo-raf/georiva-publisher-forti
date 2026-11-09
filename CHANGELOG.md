# Changelog

## 0.2.1

### ECMWF runs publish again

Every ECMWF IFS publish failed with "Precipitation is not accumulating: it falls
by 0.122 mm between consecutive steps". The data was fine. ECMWF open data packs
`tp` into 12 bits, rounding each step to a quantum of 0.122 mm, or 0.244 mm once
the wettest point on the globe passes ~500 mm. Rounded step by step, a point can
dip by one quantum between steps, and the old 0.01 mm tolerance treated that
dip as a field that is not an accumulation.

The tolerance is now 0.5 mm. The check still runs on the raw series, so a
per-step or bucketed field is still refused. Precipitation windows are then
taken over a running maximum, so a window spanning a dip reads as what fell
and never as negative rain.

No configuration change. The overlay is unchanged from 0.2.0. The sweep
retries the failed run on its own once the plugin is upgraded.

## 0.2.0

### Settings can be set from the environment

Every `GEORIVA_FORTI_*` setting can now be set in core's `.env`. Before, they
could only be set as Django settings, and core ships none of them, so on an
image-only server they were out of reach. The order is a Django setting if one
exists, then the environment, then the plugin's default. An empty value means
"use the default". In the environment, `off` turns a throttle off, as `None`
does as a Django setting. A malformed value stops the app at startup, naming
the variable.

`deploy/compose.yml` passes the variables into `georiva`,
`georiva-celery-default-worker` and `georiva-celery-processing-worker`, so
**re-fetch the overlay at `0.2.0`** when you upgrade. On the 0.1.0 overlay the
plugin runs on its defaults and the serving status page reports the compose
file as out of date.

`GEORIVA_FORTI_MAXIMUM_GRIDPOINT_DISTANCE` (metres, default 50000) is now
documented. Changing it needs a restart of `forti-rawdataforecaster`.

### The README is about installing

The README covers installing and running the plugin. The design notes, the
publication workflow, the serving details and the verification panel moved,
unchanged, to `docs/`.

## 0.1.0

First installable release. Needs GeoRiva core 0.5.0 or later, and the Forti
serving pair from `wmo-raf/forti` at `v0.8.1-wmo.1` (see `deploy/compose.yml`).

### The serving pair is pulled from ghcr

`deploy/compose.yml` now defaults to
`ghcr.io/wmo-raf/forti/rawdataforecaster:v0.8.1-wmo.1` and
`ghcr.io/wmo-raf/forti/jsonfrontend:v0.8.1-wmo.1`, rather than local image names
that only existed on the machine that built them, so an image-only server can
start the pair.

### Installing the serving pair on an image-only server

The README now says how: save `deploy/compose.yml` at your pinned tag as
`docker-compose.forti.yml` next to core's `docker-compose.yml` and list it in
`COMPOSE_FILE` in `.env`. The overlay's `FORTI_COMPOSE_VERSION` is now `0.1.0`,
matching the plugin, so the verification panel does not report a correct install
as out of date.

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
