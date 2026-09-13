"""
Delete area keys under ``_forti/`` that no publication owns and nothing serves.

Retention (``publisher.prune``) works *within* an area key, keeping the last few
versions of an area some row still has. It has no opinion about an area key that
has stopped having a row at all, and nor does anything else in this plugin: a
publication deleted by hand, or renamed by ``rename_forti_model``, leaves its
whole prefix and its ``latest/`` pointer behind, unreachable by name and
untouched by every pass. That is what this sweeps.

An area key is an orphan only when **both** authorities have let go of it:

  * **no ``FortiPublication`` claims it** — including disabled ones. Disabling is
    an operator switch, not a deletion: the row still owns the name and
    re-enabling it must not need a republish.
  * **the bucket's own ``config/rawdataforecaster.json`` does not name it.** That
    document is what ``rawdataforecaster`` is actually serving from, and it is
    deliberately *behind* the database: ``config.documents()`` withholds an empty
    ``areas`` list rather than writing one, so between a rename and the republish
    that follows it the old key is gone from the database and still live on the
    bucket. Reading only the database here would delete, at exactly that moment,
    the bytes answering every request.

The second test is why the cutover's ordering is a property of this command
rather than of the operator's memory. Run it straight after a rename and it
refuses; run it after the republish and the same invocation proceeds, because by
then the config names the new key and not the old one.

Safe by default: previews unless ``--apply``, and a config document it cannot
read stops the pass outright — "no config" is not "advertises nothing", and
treating it as such would sweep every area on the bucket at the one moment the
instance could not say otherwise.

**What it cannot see.** The sink is rooted at ``_forti/`` and
``PublicationSink.key`` refuses a path that leaves it, so anything published
under the pre-M5.2 layout — ``{org}/forti/...``, one prefix per organisation — is
outside this command by construction. That layout cannot be produced by any code
in this plugin any more; an instance still carrying it needs a one-off deletion
against the bucket, not a tool.

Examples::

    georiva cleanup_forti_orphans
    georiva cleanup_forti_orphans --apply
"""

import json

from django.core.management.base import BaseCommand, CommandError

from georiva_publisher_forti import config, models
from georiva_publisher_forti.models import FortiPublication

#: Names under the sink root that are documents rather than areas. Every area key
#: is ``{org}.{slug}`` and so contains a dot; none of these does, which is what
#: the area key's dot was chosen to guarantee (``models.area_key``). The set is
#: named as well as tested for so that a reader of a listing can see what the
#: rule is *for* — deleting one of these would take out the instance's config.
RESERVED = frozenset({"config", "status", "latest"})


class Command(BaseCommand):
    help = (
        "Delete Forti area keys on the bucket that no publication owns and the "
        "served config no longer advertises. Previews unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it, orphans are only listed.",
        )

    def handle(self, *args, **options):
        # Through the module, not a name bound at import. ``instance_sink`` is
        # the plugin's one seam onto object storage and the tests replace it
        # there (``tests/sink_isolation``); a ``from … import`` here would
        # hold the original and file real objects into the real publications
        # bucket, which has happened on this bucket before.
        sink = models.instance_sink()

        advertised = self._advertised(sink)
        owned = {
            publication.area_key
            for publication in FortiPublication.objects.select_related("collection__catalog__organisation")
        }

        on_bucket = self._area_keys(sink)
        if not on_bucket:
            self.stdout.write(f"Nothing under {sink.root} looks like an area key.")
            return

        kept, orphans = [], []
        for area_key in sorted(on_bucket):
            reason = self._keep_because(area_key, owned, advertised)
            (kept if reason else orphans).append((area_key, reason))

        for area_key, reason in kept:
            self.stdout.write(f"keep  {area_key} — {reason}")

        if not orphans:
            self.stdout.write(self.style.SUCCESS("No orphaned area keys."))
            return

        removed = 0
        for area_key, _ in orphans:
            objects = self._objects(sink, area_key)
            self.stdout.write(self.style.WARNING(f"drop  {area_key} — {len(objects)} object(s)"))
            for key in objects:
                self.stdout.write(f"        {sink.root}{key}")

            if options["apply"]:
                # include_markers because ``<key>/<version>/complete.json`` is
                # one: delete_prefix leaves markers alone by default, so a
                # version directory that cannot lose its manifest never goes
                # away and the orphan survives looking deleted. Safe here and
                # only here — nothing points at this prefix any more, which is
                # what the two tests above established.
                removed += sink.delete_prefix(area_key, include_markers=True)
                # The pointer is not under that prefix and is a separate call.
                # It is also the object it matters most to remove: it is the
                # load trigger, so a reader that finds one left behind follows
                # it to a version directory that is not there and fails at
                # startup, in an error that does not name the area.
                if sink.delete(f"latest/{area_key}"):
                    removed += 1

        if options["apply"]:
            self.stdout.write(self.style.SUCCESS(f"\nDeleted {removed} object(s)."))
        else:
            self.stdout.write(self.style.WARNING("\nPreview only. Re-run with --apply to delete."))

    def _advertised(self, sink) -> frozenset:
        """The area keys ``rawdataforecaster`` is currently loading.

        Read off the bucket rather than rebuilt from the database, because the
        point of the reading is precisely that the two can differ — and when they
        do, this is the one that describes what is answering requests.
        """
        try:
            raw = sink.read_bytes(config.RAWDATAFORECASTER_PATH)
        except FileNotFoundError:
            # Nothing has ever been published here. Then nothing is serving, and
            # the database alone decides.
            return frozenset()
        except Exception as exc:
            raise CommandError(
                f"Could not read {sink.root}{config.RAWDATAFORECASTER_PATH} ({exc}). Refusing to "
                f"sweep: without it there is no way to tell an area nothing serves from an area "
                f"this command simply could not ask about."
            ) from exc

        try:
            document = json.loads(raw)
            areas = document["areas"]
            return frozenset(str(area) for area in areas)
        except Exception as exc:
            raise CommandError(
                f"{sink.root}{config.RAWDATAFORECASTER_PATH} does not name an area list ({exc}). "
                f"Refusing to sweep: a document this command cannot read is not a document that "
                f"advertises nothing."
            ) from exc

    def _area_keys(self, sink) -> set:
        """Every area key with bytes on the bucket, pointer-only ones included.

        ``sink.children()`` finds the prefixes that hold objects; the pointers
        under ``latest/`` are listed separately because an area whose data has
        been pruned out from under its marker has no prefix left and is the most
        important orphan of the lot.
        """
        keys = {name for name in sink.children() if name not in RESERVED and "." in name}
        keys.update(name.rsplit("/", 1)[-1] for name in sink.list_keys("latest") if "." in name.rsplit("/", 1)[-1])
        return keys

    def _keep_because(self, area_key, owned, advertised) -> str | None:
        if area_key in owned:
            return "a publication owns this name"
        if area_key in advertised:
            return "still advertised by the config on the bucket — republish first, then sweep"
        return None

    def _objects(self, sink, area_key) -> list:
        objects = sorted(sink.list_keys(area_key))
        pointer = f"latest/{area_key}"
        if sink.exists(pointer):
            objects.append(pointer)
        return objects
