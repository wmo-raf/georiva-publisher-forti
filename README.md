# georiva-publisher-forti

Publishes a GeoRiva forecast collection as met.no **Forti internal-format** point
data, so a point forecast can be served as **locationforecast 2.0**.

```
GET /api/forecast/?lat=-1.2864&lon=36.8172
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
   (core, ADR 0026)     (here)      {org}/forti/            (met.no, Go)         (met.no, Go)
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
{org}/forti/
├── latest/<area>                  ← the load trigger. Polled every 3 s.
├── jsonformat.json                ← generated, org-level union
└── <area>/<version>/
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

`version` is `ref_epoch_seconds × 100 + revision`. `rawdataforecaster` reloads
only on a **strictly greater** version (`forecast.go:147`), so a plain reference
time would mean a corrected republish is ignored forever by every already-running
instance while a restarting one picks it up: two instances, same version,
different data, no error anywhere. Keying on the reference time first means a
backfilled older run can never outrank a newer one.

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

- **Collection** — must be `public`. A Forti reader presents no credential, so
  there is nobody to check a restricted collection against.
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
| `refresh_forti_jsonformat` | `georiva-default` | hourly |
| `prune_forti_publications` | `georiva-default` | daily |

Nothing goes on `georiva-ingestion`, which runs one pool process and admits fetch
and extraction only (ADR 0025). A publish is derived from data that is already
published and already servable, so nothing a reader can observe waits on it.

Retention keeps **5 versions per area**, count-based, and never the version
`latest/<area>` names however old it is — an area whose feed has stopped still
has a reader following its last good version.

## Tests

```bash
make dev-test TEST_ARGS="georiva_publisher_forti"
```

`test_publisher.py` is the one that matters: real GeoTIFFs in, a real sink out,
and the values read back the way `rawdataforecaster` reads them — by ordinal and
`slice_from`. It also holds the ordering property, by watching the bucket at the
moment each object lands.
