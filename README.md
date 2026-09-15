# georiva-publisher-forti

Publishes a GeoRiva forecast collection as met.no **Forti internal-format** point
data, so a point forecast can be served as **locationforecast 2.0**.

```
GET /api/forecast/                                   # the models you may see
GET /api/forecast/ecmwf-ifs/?lat=-1.2864&lon=36.8172 # one point out of one model
```

## Why

GeoRiva stores one COG per variable per timestep. That is the right shape for a
map tile and the worst possible shape for a point query. Measured against the
assets on disk:

| | value |
|---|---|
| COG shape | global 0.25°, 1440 × 721, float32, 256×256 tiles, ~1.76 MB each |
| A point query today | **~950 range reads ≈ 93 MB**, across 14 API calls |
| The same point out of Forti | **~2 KB**, one read |
| A whole Kenya area, every parameter, every step | ~2.7 MB |
| What it costs to build | ~748 windowed reads ≈ 72 MB, **once per run** |

The publish side gets the good half of that trade: Kenya at 0.25° falls inside a
*single* 256×256 COG tile, so reading the whole area out of one COG is one
windowed read of about 96 KB.

The API cannot be lifted away from the store — the integration point is the
ingestion pipeline, not the API layer.

## How it fits

```
RunIngestion closes ──▶ publish ──▶ georiva-publications ──▶ rawdataforecaster ──▶ jsonfrontend
   (core, ADR 0026)     (here)      _forti/                 (met.no, Go)         (met.no, Go)
```

The build discipline — six states, a claim taken at dispatch, stale-lock
recovery, the input fingerprint, the durable attempt log — comes from core's
`core.build_discipline` (ADR 0027). So does `PublicationSink`, and with it the
rule this plugin is built around:

> **The engine writes completion markers last, and the writer never writes them.**

A reader polls for a marker and loads whatever it names, so a marker that appears
before its bytes is a reader loading a partial dataset — with nothing anywhere
reporting an error, because from the reader's side the marker was a promise. The
sink refuses a marker path from `write()` and takes them only through
`publish_markers()`, after the bytes are staged.

## What it writes

```
_forti/                              ← rawdataforecaster's ?prefix=
├── latest/<org>.<slug>              ← the load trigger. Polled every 3 s.
├── config/jsonformat.json           ← generated, instance-wide union
├── config/rawdataforecaster.json    ← every published area on the instance
├── status/<module>.json             ← written by the services, pushed up
└── <org>.<slug>/<version>/
    ├── complete.json
    └── <md5(lat||lon)>/
        ├── latitude   float32[n_points]
        ├── longitude  float32[n_points]
        ├── data       int16[n_points × n_values]  little-endian
        └── meta.json
```

`data` is **point-major**: for each point, every parameter's whole series
concatenated in `slice_from` order. A reader finds a value at
`point_index × number_of_points + slice_from + step` — where `number_of_points`
is, confusingly, *values per location*.

`version` is `run.version × 100 + generation`, where the run's own half is
`ref_epoch_seconds × 100 + revision` — so the whole stamp is
`ref_epoch_seconds × 10 000 + revision × 100 + generation`, ordered **model time,
then republish of that run, then configuration generation**.

`rawdataforecaster` reloads only on a **strictly greater** version
(`forecast.go:293`), and each term answers a different way of being wrong about
that. Without the **revision**, a corrected republish of one run reuses its
integer and is ignored forever by every already-running instance while a
restarting one picks it up: two instances, same version, different data, no error
anywhere. Without the **generation**, the same happens to a republish under a
changed *configuration*, which the run knows nothing about — and the verification
panel reports published, available and loaded all equal over a real disagreement.
Keying on the reference time first means a backfilled older run can never outrank
a newer one.

The generation resets to 0 on each new run — the term above it has moved, so a
new run at 0 already outranks any generation of the one before it — and
publishing at `generation = 100` is **refused** rather than wrapped, because 100
is not a bigger number than 99 here: it is precisely the stamp the run's next
revision claims at generation 0. See `docs/adr/0003-the-published-version-learns-about-configuration.md`.

## The three things that cost real time

**Period windows are found on valid times, never on step counts.** `times` is the
window's **end** and `offset` is its **length in hours**, because jsonfrontend
files a value under `end − offset` (`encode.go:180`). ECMWF is 3-hourly to 144 h
and 6-hourly beyond, so a 6-hour window is *two steps early in the run and one
step later on*. A fixed step count is correct for exactly one half of every run —
and getting it wrong files values under the wrong timestep with every layer
reporting success. See `windows.py`.

**Runs are ragged.** Variables of one reference time ingest independently and
finish at different step counts — 13 to 16 of 16 observed live. The publisher
publishes the **intersection**: a step some variables have and others do not is a
partial forecast, and the format has no way to express one. See `planner.py`.

**The format has no missing-value convention.** A NaN encodes as `-32768` and
decodes as a plausible `-3276.8`. So only points that have data are published
(the grid is an unstructured point list, not a raster), and packing refuses a NaN
outright rather than writing one. See `writer.py`.

A fourth, smaller one: **scale factor 0 decodes as 0.1**, not 1 (`values.go:31`).
Every parameter states its factor explicitly; for the weather symbol, `1.0` is
what makes code 46 stay 46 instead of becoming 4.6 and then nothing.

## Parameter map

met.no's **production** vocabulary, read from the `jsonformat.json` baked into
their shipped image — not `forti-prep`'s, which emits `precipitation_amount_6h`
and `air_temperature_max`.

| GeoRiva | Forti internal | locationforecast | group | offset |
|---|---|---|---|---|
| `2t` | `air_temperature_2m` | `air_temperature` | instant | 0 |
| `2d` | `dew_point_temperature_2m` | `dew_point_temperature` | instant | 0 |
| `msl` | `air_pressure_at_sea_level` | `air_pressure_at_sea_level` | instant | 0 |
| `wind_speed_10m` | `wind_speed_10m` | `wind_speed` | instant | 0 |
| `wind_dir_10m` | `wind_from_direction_10m` | `wind_from_direction` | instant | 0 |
| `10fg` | `wind_speed_of_gust` | `wind_speed_of_gust` | instant | 0 |
| `tcc` | `cloud_area_fraction` | `cloud_area_fraction` | instant | 0 |
| `2t`+`2d` → derived | `relative_humidity_2m` | `relative_humidity` | instant | 0 |
| `tp` → derived | `precipitation_amount_acc1h` | `precipitation_amount` | next_1_hours | 1 |
| `tp` → derived | `precipitation_amount_acc6h` | `precipitation_amount` | next_6_hours | 6 |
| `2t` → derived | `air_temperature_2m_max6h` | `air_temperature_max` | next_6_hours | 6 |
| `2t` → derived | `air_temperature_2m_min6h` | `air_temperature_min` | next_6_hours | 6 |
| derived | `weather_symbol` | `summary.symbol_code` | next_1_hours | 1 |
| derived | `weather_symbol_6h` | `summary.symbol_code` | next_6_hours | 6 |
| derived | `weather_symbol_12h` | `summary.symbol_code` | next_12_hours | 12 |

A parameter is published only if the run's own time index can express its window,
so a 3-hourly feed publishes the 6- and 12-hour series and none of the 1-hour
ones, without anybody configuring that.

Units are **not converted** — Forti reads them from `meta.json` (`encode.go:88`)
without interpreting them. The planner instead refuses a variable whose unit is
not the one the map publishes: a collection retuned from `°C` to `K` would
otherwise publish a number wrong by 273 under a label that says celsius. Compared
through pint, so `°C` and `degC` agree and `K` does not.

## Derivations

Computed **after** the transpose, over a contiguous per-point time series — a few
numpy operations on an array that is already in memory. As derivation-engine
recipes they would cost roughly 600 MB of COGs per run and five more Icechunk
repos, to produce map layers nobody has asked for.

The weather symbol reuses met.no's threshold tables **verbatim**, because the
numeric codes have to match what `go-weathersymbol` resolves. Two things from
that file are deliberately not reused: the day/night bit, which `forti-prep`
takes from a hardcoded UTC+02:00 (here it is solar elevation at the window's
midpoint), and `_get_fog_code`, which smooths the values and then thresholds the
unsmoothed ones.

Precipitation windows use `acc[end] − acc[start]`, **not** `np.diff(acc, n=6)` —
the sixth-order finite difference, which `forti-prep`'s `convert.py:128` has used
since it was written. The length coincidentally matches, so its own assert passes.

## Configuration

One publication per collection, in the Wagtail admin under **Forti publications**:

- **Collection** — must be a **forecast** collection, and the chooser offers no
  others. Only a forecast collection has run boundaries; nothing opens a
  `RunIngestion` for any other kind, so a publication over one would wait forever
  for a run that never comes. The rule is in `clean()` as well as in the chooser,
  so a posted id meets it too. An `internal` collection is refused for a
  different reason: it is a derivation intermediate, not a dataset. `private` is
  fine — the reader holds no credential, but the Django view in front of it does
  know who is asking (D18).
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

Retention keeps **5 versions per area**, count-based, and never the version
`latest/<area>` names however old it is — an area whose feed has stopped still
has a reader following its last good version.

### Republishing one run after a configuration change

A run's version cannot express "same run, different bytes", so the publication
carries a **generation** beside it. Raise it by one and republish:

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

## Deploying the serving pair

`deploy/compose.yml` is an **overlay** on core's compose, not a stack of its own.
Fetch it at the tag you pinned for this plugin in `plugins.toml` and apply it
from core's repository root:

```bash
docker compose -f docker-compose.yml \
               -f dev-plugins/georiva-publisher-forti/deploy/compose.yml \
               up -d
```

It is static. One `rawdataforecaster` + `jsonfrontend` pair serves the whole
instance (D14): the fork takes a per-request `areas` filter and reports the
answering area back, so the tenant boundary is what the request names and is
checked on every response, rather than which process was asked. There is nothing
left that varies with the tenant set, so there is nothing to generate.

### The images

`forti-rawdataforecaster:v0.8.1-wmo.1` and `forti-jsonfrontend:v0.8.1-wmo.1`,
built from `wmo-raf/forti` `develop`. The tag is the upstream release plus the
fork's revision, so it says both what it is and that it is not stock.

There was an earlier pair tagged **`:spike`**, built during M0 from the same
sources at a different commit. Both tags, and the two stopped containers still
holding them, were removed from the dev daemon during M5.9's cutover, because
`:spike` names no commit anybody can reconstruct and an image whose provenance is
gone is one somebody eventually starts. If you find either on a daemon, it
predates this file — delete it rather than run it.

Until the images come from CI, nothing runs `govulncheck` or a build matrix over
the fork's commits. That is a known gap, not an oversight.

Neither service publishes a port. The `auth_request` gate cannot front them, so
the plugin's own `/api/forecast/{model}/` view is the only way in.

## Serving

The route reaches `/api/` through core's plugin URL hook: core discovers this
package's `api_urls` module and includes it, and knows nothing else about a point
forecast (D19). Everything the view needs — the upstream's name, the timeout, the
throttle rate, the cache lifetime — is read here with a default that lives here,
so core ships no `GEORIVA_FORTI_*` setting at all.

| setting | default | |
|---|---|---|
| `GEORIVA_FORTI_JSONFRONTEND_HOST` | `forti-jsonfrontend` | the service name in `deploy/compose.yml` |
| `GEORIVA_FORTI_JSONFRONTEND_PORT` | `8080` | |
| `GEORIVA_FORTI_TIMEOUT` | `5` | seconds; jsonfrontend gives its own upstream 1.5 s |
| `GEORIVA_FORTI_FORECAST_THROTTLE_RATE` | `"60/min"` | per caller per organisation; `None` disables |
| `GEORIVA_FORTI_FORECAST_MAX_AGE` | `1800` | how long a *public* model's answer may be shared |
| `GEORIVA_FORTI_VERIFICATION_DEADLINE` | `5` | seconds the verification panel gives the bucket, all reads together |

**The model is the resource, and it is also the tenant boundary.** The path
selects one `FortiPublication`; the view sends exactly one `area={org}.{slug}`
and refuses to serve a document whose `properties.meta.area` is anything else.
Omitting the area is not "no areas" but *every organisation's areas*, so there is
one function that builds an upstream query and it raises before the socket rather
than letting an empty one reach it.

**A model you may not see is absent, not forbidden.** The listing and the detail
route ask one query — `FortiPublication.objects.visible_to(request)`, scoped to
the host's organisation — so a private model is missing from the listing *and*
404s when named, and the endpoint cannot be used to enumerate what a tenant
publishes (D18). Visibility is read from the publication **and** its collection,
because narrowing the collection later is an ordinary edit by somebody with no
reason to know a Forti publication exists.

**Only a public model's forecast is marked cacheable.** `Cache-Control: public,
max-age=…` on a successful public answer, `private, no-store` otherwise, nothing
cacheable on an error, and never on the listing — which varies by audience at a
URL that carries none. The `/api/` proxy cache stores only what an upstream marks
(core's ADR 0029) and its key carries no identity, so the marking has to mean
"safe for whoever asks next".

The reasoning, and the five things `bc94464` got right about a design that no
longer exists, are in
[`docs/adr/0001-the-model-is-the-public-resource.md`](docs/adr/0001-the-model-is-the-public-resource.md).

Two of the rules above look arbitrary from the line they are written on — the
body-parse test instead of a `Content-Type` check, and the upstream 404 mapped to
a 503 — and are not. What they cost to find, along with the
`maximum_gridpoint_distance` measurement and why the `/api/` cache key carries
`$host`, is in
[`docs/adr/0002-four-measurements-the-reader-does-not-document.md`](docs/adr/0002-four-measurements-the-reader-does-not-document.md),
which also records the run that verified this plane end to end.

A third container, the `mc` sidecar, is the whole interface between the database
and the two processes. It runs one loop in two directions:

| direction | from | to |
|---|---|---|
| down | `_forti/config/*.json` | the config volume both services read |
| up | each service's status file | `_forti/status/*.json` |

and writes its own `_forti/status/sidecar.json` carrying the compose version, the
last pass, and the sha of each config document as it landed in the volume — the
one hop of M5.8's four that nothing else can see.

`FORTI_COMPOSE_VERSION` is set in the file and deliberately not overridable.
Operators fetch a compose file by hand and install the plugin through
`plugins.toml`, so the two can drift; this is what lets the verification panel
say when they have. It is bumped with the plugin's version, and `test_compose.py`
fails if the two disagree.

## Verifying it is actually serving

**Settings → Forti serving**, in the Wagtail admin. Read-only, and visible to the
instance admin alone.

A config document makes four hops between this database and the process that
serves from it, and each one can be stuck without the next one knowing:

| hop | who writes it | where the panel reads it |
|---|---|---|
| intended | `config.documents()` over the publication rows | this database |
| bucket | `refresh_forti_config` | `_forti/config/*.json` |
| volume | the `mc` sidecar | `_forti/status/sidecar.json`, its `volume` map |
| loaded | `configwatch` in each Go process | `_forti/status/{module}.json` |

Compared by sha, each hop against the **first** rather than against the one
before it — so the page names where the chain broke instead of showing a run of
crosses. Beside it: the areas `rawdataforecaster` is actually holding and at
which version, the sidecar's last pass, and the compose file's version against
the installed plugin's.

Three distinctions the page is careful about, because collapsing any of them
turns it into a page that lies in the case it exists for:

- **"not yet" is not "could not read".** An instance that has not cut over and
  an object store that has stopped answering are both "no sha" from Django's
  side. Every remote read is one of *present / absent / unreachable*.
- **A document GeoRiva declines to write is not a missing one.** An empty
  `parameters` map is fatal to `jsonfrontend` and an empty `areas` list to
  `rawdataforecaster`, so neither is ever written — which means "leave what is
  on the bucket alone", and the panel says so rather than rendering a mismatch.
- **`loaded_sha` is what the process last *read*, not what it is serving.**
  `configwatch` records the digest whatever the outcome, and a rejected document
  leaves the previous configuration running. A fourth hop carrying the intended
  sha with `ok: false` is therefore a *refusal*, and is rendered as one.

Freshness is the `loaded_at` / `checked_at` timestamp inside each document, never
the object's modification time: the sidecar re-uploads every pass whether or not
anything moved. The bucket's copy of a status file therefore lags by up to
`FORTI_SYNC_INTERVAL` (60 s by default).

Read-only is a decision, not a limitation (D23). The database is the single
authority and `refresh_forti_config` rewrites the bucket from it every five
minutes, so a form here would be reverted within one tick while still showing
what somebody typed.

Every figure on the page is instance-wide — `rawdataforecaster.json` names every
organisation's area keys, one sha describes one document governing every tenant —
which is why it is the instance admin's page and not an organisation's. An
organisation administrator consequently cannot see whether their own model is
resident; that question belongs beside the publication, whose organisation is
known.

The reads are done on request, all of them inside one worker thread under one
deadline (`GEORIVA_FORTI_VERIFICATION_DEADLINE`, 5 s), and nothing is cached. A
page that says it could not ask beats one that holds an admin worker through
botocore's retry ladder.

## Tests

```bash
make dev-test TEST_ARGS="georiva_publisher_forti"
```

`test_publisher.py` is the one that matters: real GeoTIFFs in, a real sink out,
and the values read back the way `rawdataforecaster` reads them — by ordinal and
`slice_from`. It also holds the ordering property, by watching the bucket at the
moment each object lands.
