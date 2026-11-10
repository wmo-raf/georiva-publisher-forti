# georiva-publisher-forti

Publishes a GeoRiva forecast collection as met.no **Forti internal-format** point
data, so a point forecast can be served as **locationforecast 2.0**.

```
GET /api/forecast/                                   # the models you may see
GET /api/forecast/ecmwf-ifs/?lat=-1.2864&lon=36.8172 # one point out of one model
GET /api/forecast/ecmwf-ifs/places/                  # the latest run, told for its towns
GET /api/forecast/ecmwf-ifs/places/?time=2026-11-10T18:00:00Z  # one time, as GeoJSON
```

`places/` joins core's town gazetteer (`georiva import_towns KE …`) onto the
point answers: the instance-wide towns plus the owning organisation's own,
inside the model's bbox. Each town carries the weather symbol and precipitation
for the finest window the model publishes (`window=PT1H|PT6H|PT12H` picks
another), with the temperature and wind at the window's start. The matrix is
built on the first request after a publish, once the reader holds the new run,
and cached until the next; until then the route answers 503 with `Retry-After`. Both forms carry the publication's `bbox` (west, south, east, north).

A point query against GeoRiva's COGs costs ~950 range reads (~93 MB). The same
point out of Forti costs one read of ~2 KB. The plugin pays for that once per run,
when the run closes:

```
RunIngestion closes ──▶ publish ──▶ georiva-publications ──▶ rawdataforecaster ──▶ jsonfrontend
   (core)               (this plugin) _forti/               (met.no, Go)         (met.no, Go)
```

It has two parts, and a working install needs both:

- **the plugin**, a Python package that runs inside GeoRiva: the admin, the
  publish tasks and the `/api/forecast/` route;
- **the serving pair**, `rawdataforecaster` and `jsonfrontend`, plus a small `mc`
  sidecar that feeds them their config. These run as extra containers beside
  GeoRiva's own and are defined in [`deploy/compose.yml`](deploy/compose.yml).

## Requirements

- **GeoRiva core 0.5.0 or later.** On an older core the plugin does not import.
  The `places/` route needs a core with the town gazetteer (`Town`, after
  0.5.0); on 0.5.0 everything else works and `places/` fails alone.
- The host must be able to pull `ghcr.io/wmo-raf/forti/rawdataforecaster` and
  `ghcr.io/wmo-raf/forti/jsonfrontend` (public packages, or `docker login ghcr.io`).

## Installing

### On a server (image-only, core's `deploy/`)

All three steps run in core's `deploy/` directory, beside `docker-compose.yml`.
Use the same plugin tag in steps 1 and 2.

**1. Install the plugin.** Add it to `plugins.toml`:

```toml
[[plugins]]
name = "GeoRiva Forti Publisher"
git  = "https://github.com/wmo-raf/georiva-publisher-forti.git"
tag  = "0.2.1"
```

**2. Add the serving pair.** The plugin is installed into the `georiva-plugins`
volume, so its compose file is not on the server's disk. Fetch it at the same tag:

```bash
curl -fsSL -o docker-compose.forti.yml \
  https://raw.githubusercontent.com/wmo-raf/georiva-publisher-forti/0.2.1/deploy/compose.yml
```

**3. Tell compose to read it.** In `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.forti.yml
```

Setting `COMPOSE_FILE` turns off compose's automatic loading of
`docker-compose.override.yml`, so list it yourself if you have one, and leave it
out if you don't.

Then deploy as usual:

```bash
./deploy.sh <core version>
```

This installs the plugin and pulls and starts `forti-config-sync`,
`forti-rawdataforecaster` and `forti-jsonfrontend` with the rest of the stack.
None of them publishes a port. The only way in is the plugin's `/api/forecast/`
route.

**Upgrading.** Bump the tag in `plugins.toml`, then from core's `deploy/`:

```bash
./deploy.sh plugins
```

It reinstalls, reports `~ georiva_publisher_forti 0.2.0 → 0.2.1`, and restarts
the services that load the plugin. A bare `docker compose up -d` is not enough:
it installs the new version but leaves the running workers on the old one.
`./deploy.sh plugins` needs a core version from after ADR 0040.

When the CHANGELOG says the overlay changed, also re-run step 2 at the new tag:
nothing re-fetches the compose file for you. If the two drift apart, the serving
status page (below) reports it.

### In a development checkout

Clone the plugin into core's `dev-plugins/` and apply the overlay from core's
repository root:

```bash
docker compose -f docker-compose.yml \
               -f dev-plugins/georiva-publisher-forti/deploy/compose.yml \
               up -d
```

## Publishing a model

In the Wagtail admin, **Publications → Forti → Add**:

- **Collection**: a **public forecast** collection. Other kinds are not offered.
  A private collection saves but does not publish.
- **Area**: the name readers ask for, unique within the organisation.
- **Extent**: give it a margin past the border. The point list is pinned at the
  first publish, so a later build with a different grid is refused.

Saving lands you on the **Mapping** page. It pairs eight slots (`2t`, `2d`, `msl`,
`tp`, …) with the collection's variables, auto-matched by slug. Check each row.
No automatic check catches a variable mapped to the wrong slot when the units are
the same, for example dew point mapped to air temperature.

The publication's **Inspect** page shows **Readiness**: whether a publish would
succeed now, should wait, or needs a change first. Once ready, the next closed run
publishes it, or the five-minute sweep does.

Details: [docs/publications.md](docs/publications.md).

## Checking that it serves

**Settings → Forecast serving status** (instance admin only) follows the config
from the database to the bucket, then into the containers' config folder and
into each running process. It tells you where the chain stops and what to do
about it. That includes "update the compose file to the tag for this plugin
version" when step 2 above was missed on an upgrade.

Each organisation sees a **Resident** column on its publications listing, showing
whether the version the reader holds matches the version published.

Details: [docs/verification.md](docs/verification.md).

## Settings

All optional. Set them in core's `.env`. The overlay passes them into the
`georiva` web app and the default and processing workers. An empty value means
"use the default", and a malformed one stops the app at startup, naming the
variable. A Django setting of the same name, if one exists, takes precedence.

| variable | default | |
|---|---|---|
| `GEORIVA_FORTI_JSONFRONTEND_HOST` | `forti-jsonfrontend` | the service name in `deploy/compose.yml` |
| `GEORIVA_FORTI_JSONFRONTEND_PORT` | `8080` | |
| `GEORIVA_FORTI_TIMEOUT` | `5` | seconds |
| `GEORIVA_FORTI_FORECAST_THROTTLE_RATE` | `60/min` | anonymous callers, per IP per organisation; `off` disables |
| `GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED` | `600/min` | signed-in callers (session or `grv_…` key); `off` disables. Give an NMHS website a key rather than raising the anonymous rate |
| `GEORIVA_FORTI_FORECAST_MAX_AGE` | `1800` | how long a *public* model's answer may be cached |
| `GEORIVA_FORTI_VERIFICATION_DEADLINE` | `5` | seconds the status page and listing give the bucket |
| `GEORIVA_FORTI_MAXIMUM_GRIDPOINT_DISTANCE` | `50000` | metres; a point farther than this from the nearest gridpoint is outside coverage. Restart `forti-rawdataforecaster` after changing it |
| `GEORIVA_FORTI_PLACES_CONCURRENCY` | `8` | parallel reader calls while a model's town forecasts are first built |
| `GEORIVA_FORTI_PLACES_WAIT` | `5` | seconds a request waits on a town build already running before answering 503 |

For example, in `.env`:

```bash
GEORIVA_FORTI_FORECAST_THROTTLE_RATE=120/min
GEORIVA_FORTI_FORECAST_MAX_AGE=900
```

Then `./deploy.sh <core version>` again, so the containers pick the values up.

The overlay also reads these for its own services, with defaults that work as they are:

| variable | default |
|---|---|
| `FORTI_RAWDATAFORECASTER_IMAGE` | `ghcr.io/wmo-raf/forti/rawdataforecaster:v0.8.1-wmo.1` |
| `FORTI_JSONFRONTEND_IMAGE` | `ghcr.io/wmo-raf/forti/jsonfrontend:v0.8.1-wmo.1` |
| `FORTI_SYNC_INTERVAL` | `60` (seconds) |

## Operations

| task | queue | cadence |
|---|---|---|
| `publish_forti_area` | `georiva-processing` | on a run closing |
| `sweep_forti_publications` | `georiva-default` | 5 minutes |
| `refresh_forti_config` | `georiva-default` | 5 minutes, and at the end of a publish |
| `prune_forti_publications` | `georiva-default` | daily; keeps 5 runs per area |

Two operations need care, and each has a section in
[docs/publications.md](docs/publications.md):

- **Republishing a run after a configuration change.** Raise the publication's
  `generation` first. Republishing without that is ignored by the reader, and
  nothing reports an error.
- **Renaming a published model.** Use `rename_forti_model` and
  `cleanup_forti_orphans`, in that order. If real consumers are attached, don't
  rename at all. Publish a second model under the new name instead.

## Further reading

- [docs/design.md](docs/design.md): what it writes, the version scheme, the format's
  traps, the parameter map, derivations
- [docs/publications.md](docs/publications.md): configuration, variable mapping,
  readiness, retention, republishing, renaming
- [docs/deployment.md](docs/deployment.md): why one static pair, the images, how the
  route serves and enforces tenancy, the sidecar
- [docs/verification.md](docs/verification.md): the serving status page, Resident,
  History, `PublicationWritten`
- [docs/adr/](docs/adr/): the decisions

## Tests

```bash
just test georiva_publisher_forti   # from core's repository root
```
