# Verifying and tracing what is served

The forecast serving status page, the Resident column, a publication's History, and the `PublicationWritten` event.

## Verifying it is actually serving

**Settings → Forecast serving status**, in the Wagtail admin. Read-only, and
visible to the instance admin alone.

The page answers one question — *is this instance serving point forecasts
through Forti, and if not, where did it stop?* — and it is written for the
operator who deployed the compose file, not for someone who knows Forti's
internals. So every sentence on it has one shape, **what is happening, then what
to do**, and the "what to do" half is drawn from a fixed list of things that
operator can actually do:

- wait for the next refresh (the reconciler runs every five minutes)
- re-publish an area, or enable a publication
- restart the Forti services
- check the bucket connection
- update the compose file to the tag for this plugin version
- check a service's logs
- nothing to do

One line at the top says either *Everything is serving as intended* or *N
problems found — see below*, counted from the same fields the sections render.
The real service names (`rawdataforecaster`, `jsonfrontend`, the `sidecar`) stay
on the page, because they are what appears in `docker ps` and in logs, but each
is introduced with its plain role: the forecast reader, the API front end, the
sync helper.

### What the page checks, and why it is shaped this way

A config document makes four hops between this database and the process that
serves from it, and each one can be stuck without the next one knowing:

| step | who writes it | where the panel reads it |
|---|---|---|
| 1. Database | `config.documents()` over the publication rows | this database |
| 2. Bucket | `refresh_forti_config` | `_forti/config/*.json` |
| 3. Config folder | the `mc` sidecar | `_forti/status/sidecar.json`, its `volume` map |
| 4. Loaded | `configwatch` in each Go process | `_forti/status/{module}.json` |

Compared by sha, each hop against the **first** rather than against the one
before it — so the page names where the chain broke instead of showing a run of
crosses. The 12-character checksum under each step is the first twelve hex
characters of that sha, with the whole digest on hover; it is there so a
support conversation has an identifier to quote, not as the first thing to
read. Beside the chains: the areas `rawdataforecaster` is actually holding and
at which version, the sidecar's last pass, and the compose file's version
against the installed plugin's.

Three distinctions the page is careful about, because collapsing any of them
turns it into a page that lies in the case it exists for:

- **"missing" is not "unreachable".** An instance that has not cut over and an
  object store that has stopped answering are both "no sha" from Django's side.
  Every remote read is one of *present / absent / unreachable*.
- **A document GeoRiva declines to write is not a missing one.** An empty
  `parameters` map is fatal to `jsonfrontend` and an empty `areas` list to
  `rawdataforecaster`, so neither is ever written — which means "leave what is
  on the bucket alone", and the panel says *not needed* rather than rendering a
  mismatch.
- **`loaded_sha` is what the process last *read*, not what it is serving.**
  `configwatch` records the digest whatever the outcome, and a rejected document
  leaves the previous configuration running. A fourth hop carrying the intended
  sha with `ok: false` is therefore *rejected*, and is rendered as one.

Two more things the page relies on but no longer explains inline:

- **Ages are the `loaded_at` / `checked_at` timestamp inside each document**,
  never the object's modification time: the sidecar re-uploads every pass
  whether or not anything moved. The bucket's copy of a status file therefore
  lags by up to `FORTI_SYNC_INTERVAL` (60 s by default), and one sync pass of
  lag at steps 3 and 4 is ordinary.
- **The area versions do not decay.** `available` is the reader's last
  *successful* listing of `_forti/latest/`, so when the listing is failing the
  page prints the reader's `store_error` above the table; without it, a store
  nobody can reach looks exactly like one holding precisely what is loaded. That
  banner is also the only place a failure to list `_forti/latest/` is visible —
  one unparseable object there takes every organisation off the air at startup.

Read-only is a decision, not a limitation (D23). The database is the single
authority and `refresh_forti_config` rewrites the bucket from it every five
minutes, so a form here would be reverted within one tick while still showing
what somebody typed.

Every figure on the page is instance-wide — `rawdataforecaster.json` names every
organisation's area keys, one sha describes one document governing every tenant —
which is why it is the instance admin's page and not an organisation's. What an
organisation administrator needs from it is on the publications listing instead;
see below.

The reads are done on request, all of them inside one worker thread under one
deadline (`GEORIVA_FORTI_VERIFICATION_DEADLINE`, 5 s), and nothing is cached. A
page that says it could not ask beats one that holds an admin worker through
botocore's retry ladder.

### Translating it

Every sentence the page renders — in the template and in `verification.py` — is
wrapped in gettext, and `locale/en/LC_MESSAGES/django.po` is the catalogue.
English is the source language, so its msgstrs are empty; copy the directory to
`locale/<lang>/LC_MESSAGES/`, fill it in, and run `compilemessages`. Regenerate
after changing any user-facing text:

```sh
cd src/georiva_publisher_forti && django-admin makemessages -l en --no-obsolete -i "tests/*"
```

### Resident, on the publications listing

**Publications → Forti** carries a **Resident** column beside
**Published version**, and an organisation administrator sees it for their own
publications. Published and resident are two facts: the database says a version
was written, and `rawdataforecaster` says which version it is actually holding.
The column is the difference, in one word — *matches*, *differs*, *not published
yet*, *unreachable*, *unknown*, *no status* — with the resident version beside
it and the whole sentence on hover.

An **area row narrows safely and a configuration digest does not**, which is why
this can be an organisation's view while the panel above stays the instance
admin's: a row is one area key, one version and one organisation, where a sha
describes one document governing every tenant. `verification.resident_areas()`
reads the same status document the panel reads, through the same guard and under
the same deadline, and answers one publication at a time — so a cell can only
ever ask about the area its own row already holds.

The status document lists every area at once, so the whole listing is served by
**one** read rather than one per row: a read per row would put object storage's
deadline on the page instead of on the read.

### History, on a publication's inspect page

**Publications → Forti → *one model* → Inspect** ends with a **History** section:
every publish attempt and every retention pass, newest first, with what it did
and — for a failure — the error beside the attempt rather than in a log search.
The rows have always been recorded; until now nothing rendered one, and the
publication itself holds only the *latest* state, a failed build overwriting the
previous error in place. So the panel is what tells a first failure from a week
of them, and what shows a run getting smaller before somebody complains.

Two things it says that the columns do not:

- **an absent figure is one the attempt never reached**, not a count of zero. No
  publish this plugin can complete writes zero objects or transposes zero
  points, so a zero at the database means the attempt died before counting —
  and printing it would describe a run that collapsed to nothing;
- **"nothing to do" is not "wrote nothing"**. A publish that finds its
  fingerprint unmoved returns before writing, which is the ordinary outcome of
  the re-queue button on a current publication.

It reads the database and nothing else — no status document, no bucket. Every
other Forti surface reads the serving plane, and a page behind object storage's
deadline is one that cannot be opened while the bucket is slow.

What bounds the table is **retention**: the daily pass deletes rows older than
thirty days, and thirty days of the busiest plausible cadence is under two
hundred. The 250-row ceiling above that is a guard on the deployment where that
pass is *not* running — rows then accumulate until somebody notices, and the page
they would notice on must not be the one that stops loading. When it bites, the
table says how many rows it is not showing.

## Traceability: `PublicationWritten`

Every publish that writes a version records a **`PublicationWritten`** Domain
event, in the same transaction as `mark_ready` and after the markers: the
publication's slug, area key and root, the run's collection, reference time and
revision, the version `latest/<area key>` now names, and how much was written.
It is the recorded answer to "which run, at which revision, is the app
showing" — the publication row is overwritten by the next publish, the event is
not, and its table rejects `UPDATE` and `DELETE` in Postgres itself.

It belongs to the chain that produced the run. The run-closed receiver
dispatches inside the context after the `RunIngestionClosed` core hands it, so
the publish carries the run's Correlation across the queue and names the close
as its cause; the event then shows in core's **Timeline** for that chain, beside
the fetch and the ingestions that led to it, scoped to the organisation like
every other event. A publish the sweep picks up instead continues the chain of
the run it publishes — that run's latest close — under the sweep's Actor
(`sweep:sweep_forti_publications`); not the publication's last chain, which
after a lost or overtaken publish belongs to the run before. A reopened run records nothing — it only
marks stale — and neither does a publish that finds nothing new to write.

All four tasks are on core's task base classes — the publish a `ChainedTask`,
the sweep a `SweepTask`, the config reconciler and the retention pass
`EntryTask`s. A plainly declared task would keep working and silently drop the
chain.
