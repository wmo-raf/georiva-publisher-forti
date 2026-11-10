# The serving pair in detail

The install steps are in the [README](../README.md#installing). This is the background: why the pair is static, where the images come from, how the API route serves, and what the sidecar does.

## One pair for the instance

It is static. One `rawdataforecaster` + `jsonfrontend` pair serves the whole
instance (D14): the fork takes a per-request `areas` filter and reports the
answering area back, so the tenant boundary is what the request names and is
checked on every response, rather than which process was asked. There is nothing
left that varies with the tenant set, so there is nothing to generate.

## Settings passed into core's services

Core ships no `GEORIVA_FORTI_*` setting (D19), and its backend services take an
explicit variable list with no `env_file`. A line in `.env` therefore reaches
Django only if something names it. The overlay does that: an
`x-forti-settings` anchor lists every name in `forti_settings.SETTINGS` as
`${X:-}`, merged into `georiva`, `georiva-celery-default-worker` and
`georiva-celery-processing-worker`. `test_compose.py` keeps the two lists equal.

This is the one place the overlay names core's services. If core renames one,
compose finds a service here with no image and `up` fails loudly.

## The images

`ghcr.io/wmo-raf/forti/rawdataforecaster:v0.8.1-wmo.1` and
`ghcr.io/wmo-raf/forti/jsonfrontend:v0.8.1-wmo.1`, published by `wmo-raf/forti`'s
release workflow when it is given the `v0.8.1-wmo.1` tag on `develop`. The tag is
the upstream release plus the fork's revision, so it says both what it is and
that it is not stock. A server pulls them; nothing builds on it. Override with
`FORTI_RAWDATAFORECASTER_IMAGE` / `FORTI_JSONFRONTEND_IMAGE`.

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
| `GEORIVA_FORTI_FORECAST_THROTTLE_RATE` | `"60/min"` | anonymous callers, per IP per organisation; `None` disables |
| `GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED` | `"600/min"` | signed-in callers (session or `grv_…` key), per user per organisation; `None` disables. Give an NMHS website a key rather than raising the anonymous rate |
| `GEORIVA_FORTI_FORECAST_MAX_AGE` | `1800` | how long a *public* model's answer may be shared |
| `GEORIVA_FORTI_VERIFICATION_DEADLINE` | `5` | seconds the verification panel and the publications listing give the bucket, all reads together |
| `GEORIVA_FORTI_PLACES_CONCURRENCY` | `8` | parallel reader calls while a model's town forecasts are first built |
| `GEORIVA_FORTI_PLACES_WAIT` | `5` | seconds a request waits on a town build already running before answering 503 |

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
[`adr/0001-the-model-is-the-public-resource.md`](adr/0001-the-model-is-the-public-resource.md).

Two of the rules above look arbitrary from the line they are written on — the
body-parse test instead of a `Content-Type` check, and the upstream 404 mapped to
a 503 — and are not. What they cost to find, along with the
`maximum_gridpoint_distance` measurement and why the `/api/` cache key carries
`$host`, is in
[`adr/0002-four-measurements-the-reader-does-not-document.md`](adr/0002-four-measurements-the-reader-does-not-document.md),
which also records the runs that verified this plane end to end: the cutover,
and the re-run after the version scheme changed, which remapped a slot on a live
plane and watched the reader follow it.

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
