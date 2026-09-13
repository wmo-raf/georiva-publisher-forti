"""
Rename a published model — the one operation ``FortiPublication`` refuses.

``_published_under_another_slug()`` raises from both ``clean()`` and ``save()``,
so neither the admin nor a shell ``.save()`` can do this, and that refusal is
right: the slug is a segment of every storage key, so a rename leaves
``latest/<org>.<old>`` pointing at bytes no retention pass looks at while
consumers go on asking for the old name and the new one has nothing under it.

**This command is not a way past that guard — it is a way to make the guard's
own precondition false.** The guard fires on a row that *has published* under
another name. So the rename and the un-publishing happen in one transaction:
afterwards the row says, truthfully, that it has published under no name at all.
There is no instant at which the database claims a version under a slug whose
bytes are not on the bucket, which is the state the guard exists to prevent. An
ordinary ``.save()`` rename is refused before this command and again after the
next publish; only the row's published state, which is ``editable=False`` and
writable from nowhere else, moves.

The bytes are **not** touched here, and the order is the reason:

1. ``rename_forti_model kenya ecmwf-ifs --apply``
2. republish — the run reappears under the new area key;
3. ``cleanup_forti_orphans --apply`` — the old key's bytes, now unreachable.

Deleting first would leave the instance serving nothing for the length of a
publish. Deleting after costs a second pass and nothing else, because the config
document on the bucket keeps naming the old area until the new one has bytes
(``config.documents`` withholds an empty ``areas`` list rather than writing one),
so the old name goes on being served right up to the moment it is replaced.

**With real consumers attached, do not do any of this.** A consumer's client has
the old slug hard-coded, and no ordering of ours reaches it. The supported answer
is the one ``_RENAME_REFUSED`` gives — publish a *second* publication under the
new name, serve both, and retire the old one after a deprecation window. This
command is for an instance whose model name is still wrong and whose consumers
are still nobody.

Examples::

    georiva rename_forti_model kenya ecmwf-ifs
    georiva rename_forti_model kenya ecmwf-ifs --apply
    georiva rename_forti_model kenya ecmwf-ifs --org central --apply

Model slugs are unique per organisation, so ``--org`` is needed only when two
organisations both publish a model of the current name.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_slug
from django.db import transaction

from georiva_publisher_forti.models import FortiPublication


class Command(BaseCommand):
    help = (
        "Rename a Forti model, clearing its published state so the next publish "
        "writes under the new area key. Previews unless --apply is given."
    )

    def add_arguments(self, parser):
        parser.add_argument("current", metavar="SLUG", help="The model's current slug.")
        parser.add_argument("new", metavar="NEW_SLUG", help="The slug it should answer to.")
        parser.add_argument(
            "--org",
            metavar="SLUG",
            help="Narrow to one organisation. Needed only if the current slug is ambiguous.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually rename. Without it, the rename is only described.",
        )

    def handle(self, *args, **options):
        current = options["current"]
        new = options["new"]

        if current == new:
            raise CommandError(f"{current!r} is already the model's name.")

        try:
            validate_slug(new)
        except ValidationError:
            raise CommandError(
                f"{new!r} is not a slug. The area key is {{org}}.{{slug}} and is decoded on its "
                f"first dot, so a slug carrying one of its own leaves the key ambiguous — and a "
                f"slash leaves it more than one path segment, which makes GetGridInfo skip every "
                f"grid without an error."
            ) from None

        publication = self._one(current, options["org"])
        organisation = publication.organisation
        old_key = publication.area_key
        new_key = f"{organisation.slug}.{new}"

        if publication.lock_is_live:
            raise CommandError(
                f"{old_key} is building right now (claim {publication.locked_by!r}). That build "
                f"resolved the area key when it loaded the row and will write under {old_key!r}, "
                f"then stamp a published_version the new name has no bytes for. Wait for it, or "
                f"clear the claim, then run this again."
            )

        if self._taken(publication, new, organisation):
            raise CommandError(
                f"{organisation.slug} already publishes a model called {new!r}. The slug is the "
                f"name a consumer asks by and a segment of every storage key: two models with one "
                f"name means one of them is unreachable."
            )

        self._describe(publication, new, old_key, new_key)

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("\nPreview only. Re-run with --apply to rename."))
            return

        with transaction.atomic():
            renamed = (
                FortiPublication.objects.filter(pk=publication.pk, slug=current)
                .select_for_update()
                .update(
                    slug=new,
                    # Everything below is what "has published" is made of, and it
                    # all describes bytes under the *old* key. Clearing it in the
                    # same statement as the slug is what keeps the row honest at
                    # every instant a reader could observe it:
                    #
                    #   published_version  — rdfconfig.servable's filter, and the
                    #     guard's own precondition;
                    #   input_fingerprint  — is_up_to_date's, which knows nothing
                    #     about the slug and would otherwise skip the republish;
                    #   status             — PENDING is what the sweep picks up.
                    #
                    # grid_id and point_count stay. A rename does not move a
                    # gridpoint, and keeping the pin makes the republish check
                    # that the point list is still the one every stored ordinal
                    # was written against.
                    published_version=None,
                    published_reference_time=None,
                    published_step_count=0,
                    published_parameters=[],
                    input_fingerprint="",
                    status=FortiPublication.Status.PENDING,
                    built_at=None,
                    error="",
                    locked_at=None,
                    locked_by="",
                )
            )

        if not renamed:
            raise CommandError(f"{old_key} changed underneath this command. Nothing was renamed.")

        self.stdout.write(self.style.SUCCESS(f"\nRenamed {old_key} → {new_key}."))
        self.stdout.write(
            "\nThe model is now unpublished and serves nothing under its new name. Next:\n"
            f"  1. republish it — the sweep will, within five minutes, or dispatch it by hand;\n"
            f"  2. then: georiva cleanup_forti_orphans --apply\n"
            f"\nUntil (1) lands, the config on the bucket goes on naming {old_key} and the old\n"
            "bytes go on answering. That is the ordering working; cleanup_forti_orphans reads\n"
            "the same document and will refuse to delete them until it has stopped."
        )

    def _one(self, slug, org):
        """The single publication of that name, or a refusal that says why."""
        matches = list(
            FortiPublication.objects.filter(slug=slug)
            .select_related("collection__catalog__organisation")
            .order_by("pk")
        )
        if org:
            matches = [p for p in matches if p.organisation.slug == org]

        if not matches:
            scope = f" in {org}" if org else ""
            raise CommandError(f"No Forti model is called {slug!r}{scope}.")
        if len(matches) > 1:
            owners = ", ".join(sorted(p.organisation.slug for p in matches))
            raise CommandError(f"{slug!r} is published by more than one organisation ({owners}). Pass --org.")
        return matches[0]

    def _taken(self, publication, new, organisation) -> bool:
        """Whether the new name is in use — within this organisation only.

        Slugs are unique per organisation and the area key carries the owner, so
        ``ke-kmd.ecmwf-ifs`` and ``central.ecmwf-ifs`` are different keys. A check
        across the instance would be reading the model name as instance-wide,
        which is exactly what the dot in the area key exists to avoid.
        """
        return (
            FortiPublication.objects.filter(
                slug=new,
                collection__catalog__organisation=organisation,
            )
            .exclude(pk=publication.pk)
            .exists()
        )

    def _describe(self, publication, new, old_key, new_key):
        """What changes, and what stops being reachable when it does."""
        self.stdout.write(f"{publication.collection.slug}: {old_key} → {new_key}")
        self.stdout.write(f"  route      /api/forecast/{publication.slug}/ → /api/forecast/{new}/")
        self.stdout.write(
            f"  published  version {publication.published_version} → none (status {publication.status} → pending)"
        )

        # ``FortiPublication.sink`` calls ``models.instance_sink`` through the
        # module, which is the seam the tests replace. Reaching for
        # ``instance_sink`` by an imported name here would hold the original
        # and list the real publications bucket from a test run.
        sink = publication.sink()
        try:
            orphaned = sorted(sink.list_keys(old_key))
            pointer = f"latest/{old_key}"
            if sink.exists(pointer):
                orphaned.append(pointer)
        except Exception as exc:
            # The bucket is not this command's dependency — the rename is a
            # database operation and stays one. Not being able to *describe* the
            # cost is worth saying out loud and is not worth refusing over.
            self.stdout.write(self.style.WARNING(f"  bucket     could not be listed ({exc})"))
            return

        if not orphaned:
            self.stdout.write(f"  orphaning  nothing — {old_key} has no bytes under {sink.root}")
            return

        self.stdout.write(f"  orphaning  {len(orphaned)} object(s) under {sink.root}, which nothing prunes:")
        for key in orphaned:
            self.stdout.write(f"               {sink.root}{key}")
