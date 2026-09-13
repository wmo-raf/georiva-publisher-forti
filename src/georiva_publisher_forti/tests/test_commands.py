"""The two operator commands, and the invariants they are allowed to move.

Both exist because of the same fact: the slug is a segment of every storage key,
so a rename is a storage operation wearing a database operation's clothes. The
model refuses it (``_published_under_another_slug``) and is right to. These
commands are how an operator performs one anyway — in the order that leaves
nothing serving from bytes nobody can find.

What is worth testing here is not that the commands do what they say. It is the
two refusals, because each of them is the only thing standing between an
operator and a state nothing reports:

- ``rename_forti_model`` must leave the row **unpublished**, not merely renamed.
  A row carrying ``published_version`` under a name whose bytes are not on the
  bucket is advertised by ``rdfconfig.servable`` and 404s from
  ``rawdataforecaster`` — the exact state ``serving.py``'s 404 branch was written
  to describe and cannot fix.
- ``cleanup_forti_orphans`` must refuse a key the *bucket's own config* still
  advertises, whatever the database says. That is what makes "delete after the
  republish" a property of the tool rather than of the operator's memory.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from georiva.core.publishing import CompletionMarker
from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG
from georiva_publisher_forti import config, models
from georiva_publisher_forti.models import FortiPublication
from georiva_publisher_forti.writer import completion_markers

from .factories import make_collection, make_publication
from .sink_isolation import TemporarySinkMixin

#: The organisation ``make_collection`` builds under. Every area key below is
#: derived from it rather than spelled, because the key is ``{org}.{slug}`` and
#: a test that hard-codes the organisation half is testing the fixture.
ORG = DEFAULT_TEST_ORG_SLUG


def run(command, *args, **options):
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=out, **options)
    return out.getvalue()


class RenameTests(TemporarySinkMixin, TestCase):
    """The rename itself, and the preview that prices it.

    Sink-isolated even though the rename is a database operation, because the
    *preview* is not: it lists the old area key to say what stops being
    reachable. Without the mixin those reads go to the instance's real
    publications bucket — which is how a stray ``jsonformat.json`` turned up
    under ``test-org/forti/`` once already — and the preview assertions below
    would pass on an empty listing, proving nothing.
    """

    def setUp(self):
        self.isolate_sink()
        self.sink = models.instance_sink()
        self.publication = make_publication(make_collection(), slug="kenya")
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            status=FortiPublication.Status.READY,
            published_version=178925760000,
            published_step_count=16,
            published_parameters=["air_temperature"],
            input_fingerprint="run-42",
            grid_id="dd566ea71ba587cecb5d66213e235d07",
        )

    def reread(self):
        return FortiPublication.objects.get(pk=self.publication.pk)

    def stage_area(self, area_key, version=178925760000):
        self.sink.write(f"{area_key}/{version}/grid/data", b"packed")
        self.sink.publish_markers(completion_markers(area_key, version))

    def test_a_preview_changes_nothing(self):
        output = run("rename_forti_model", "kenya", "ecmwf-ifs")

        self.assertEqual(self.reread().slug, "kenya")
        self.assertIn("--apply", output)

    def test_the_preview_names_the_bytes_it_is_about_to_orphan(self):
        """The whole cost of the operation, before it is paid.

        An operator who cannot see which keys stop being reachable has no way to
        know that the rename is the easy half. Asserted on the object keys
        themselves and not merely on the area key, which the first line of the
        preview prints whatever the bucket says — including on the branch where
        it could not be listed at all.
        """
        self.stage_area(f"{ORG}.kenya")

        output = run("rename_forti_model", "kenya", "ecmwf-ifs")

        self.assertIn(f"{ORG}.ecmwf-ifs", output)
        self.assertIn(f"_forti/{ORG}.kenya/178925760000/grid/data", output)
        self.assertIn(f"_forti/{ORG}.kenya/178925760000/complete.json", output)
        self.assertIn(f"_forti/latest/{ORG}.kenya", output)

    def test_the_preview_says_so_when_there_is_nothing_to_orphan(self):
        """A row that never published has no bytes, and the count must not lie.

        The pointer is appended only where it exists: a preview that always
        printed ``latest/<key>`` would name an object that is not there, which
        is the one line an operator would act on.
        """
        output = run("rename_forti_model", "kenya", "ecmwf-ifs")

        self.assertIn("orphaning  nothing", output)
        self.assertNotIn(f"_forti/latest/{ORG}.kenya", output)

    def test_apply_renames_and_unpublishes(self):
        run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        renamed = self.reread()
        self.assertEqual(renamed.slug, "ecmwf-ifs")
        self.assertEqual(renamed.area_key, f"{ORG}.ecmwf-ifs")
        self.assertIsNone(renamed.published_version)
        self.assertEqual(renamed.status, FortiPublication.Status.PENDING)

    def test_the_renamed_row_is_not_up_to_date_under_its_new_name(self):
        """The fingerprint has to go with the version, or nothing republishes.

        ``publish()`` returns early on ``is_up_to_date``, which reads the
        fingerprint and the status and knows nothing about the slug. Clearing
        only ``published_version`` produces a row that the guard now permits, the
        config withholds, and the publisher declines to rebuild — a cutover that
        silently stops half way.
        """
        run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        self.assertFalse(self.reread().is_up_to_date("run-42"))

    def test_the_pinned_grid_survives(self):
        """A rename does not move a gridpoint.

        Keeping the pin is what makes the republish check that the point list is
        the one every stored ordinal was written against.
        """
        run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        self.assertEqual(self.reread().grid_id, "dd566ea71ba587cecb5d66213e235d07")

    def test_the_guard_still_refuses_an_ordinary_rename_afterwards(self):
        """The escape is one command, not a hole.

        Once the renamed row has published again, ``save()`` refuses to rename it
        exactly as it refused before. If this ever passes silently, the command
        has widened the guard rather than stepped around it once.
        """
        run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)
        FortiPublication.objects.filter(pk=self.publication.pk).update(published_version=178925760000)

        republished = self.reread()
        republished.slug = "something-else"
        with self.assertRaises(Exception) as raised:
            republished.save()

        self.assertIn("cannot be renamed", str(raised.exception))

    def test_an_unknown_model_is_an_error_not_a_no_op(self):
        with self.assertRaises(CommandError):
            run("rename_forti_model", "nothing-here", "ecmwf-ifs", apply=True)

    def test_a_name_this_organisation_already_uses_is_refused(self):
        make_publication(make_collection(slug="ifs-pressure"), slug="ecmwf-ifs")

        with self.assertRaises(CommandError) as raised:
            run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        self.assertIn("already", str(raised.exception))
        self.assertEqual(self.reread().slug, "kenya")

    def test_another_organisations_name_is_not_a_collision(self):
        """Slugs are unique per organisation, and the area key carries the org.

        ``ke-kmd.ecmwf-ifs`` and ``central.ecmwf-ifs`` are different segments and
        different keys; refusing the second would be reading the model name as
        instance-wide, which is what the dot in the area key exists to avoid.
        """
        make_publication(
            make_collection(slug="ifs-surface", org_slug="ke-kmd"),
            slug="ecmwf-ifs",
        )

        run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        self.assertEqual(self.reread().slug, "ecmwf-ifs")

    def test_a_live_build_lock_is_refused(self):
        """A publish in flight holds the *old* area key in memory.

        It resolved ``area_key`` when the task loaded the row and will write
        under it, then ``mark_ready`` will stamp a ``published_version`` the new
        name has no bytes for. Renaming underneath a running build is how the
        cutover's one forbidden state gets created by accident.
        """
        from django.utils import timezone

        FortiPublication.objects.filter(pk=self.publication.pk).update(
            status=FortiPublication.Status.BUILDING,
            locked_at=timezone.now(),
            locked_by="celery-live",
        )

        with self.assertRaises(CommandError) as raised:
            run("rename_forti_model", "kenya", "ecmwf-ifs", apply=True)

        self.assertIn("building", str(raised.exception).lower())
        self.assertEqual(self.reread().slug, "kenya")

    def test_renaming_to_the_same_name_is_refused(self):
        with self.assertRaises(CommandError):
            run("rename_forti_model", "kenya", "kenya", apply=True)

    def test_a_slug_the_field_would_not_accept_is_refused(self):
        """The grammar is the SlugField's, and a dot would split the area key.

        ``central.ecmwf.ifs`` is still one path segment, so nothing would break
        *visibly* — but ``{org}.{slug}`` stops being decodable, and every reader
        of the key has to guess which dot is the separator.
        """
        with self.assertRaises(CommandError):
            run("rename_forti_model", "kenya", "ecmwf.ifs", apply=True)


class CleanupOrphanTests(TemporarySinkMixin, TestCase):
    def setUp(self):
        self.isolate_sink()
        # Through the module: ``isolate_sink`` replaces ``models.instance_sink``,
        # and a name imported at the top of this file would still be the
        # original — pointing this whole class at the real publications bucket.
        self.sink = models.instance_sink()
        self.publication = make_publication(make_collection(), slug="ecmwf-ifs")
        FortiPublication.objects.filter(pk=self.publication.pk).update(
            status=FortiPublication.Status.READY,
            published_version=178925760000,
        )

    def stage_area(self, area_key, version=178925760000):
        """One area's bytes as a publish leaves them: data, then both markers.

        Through ``publish_markers`` rather than the bucket, because the markers
        are the half that matters here — they are what ``delete_prefix`` skips by
        default and what a reader follows — and staging them by a route the
        engine does not use would test a layout no publish produces.
        """
        self.sink.write(f"{area_key}/{version}/grid/data", b"packed")
        self.sink.publish_markers(completion_markers(area_key, version))

    def advertise(self, *area_keys):
        """What the bucket's own ``rawdataforecaster.json`` currently names."""
        self.sink.write(
            config.RAWDATAFORECASTER_PATH,
            config.encode({"areas": list(area_keys)}),
        )

    def keys(self):
        return set(self.sink.list_keys())

    def test_an_area_no_row_claims_and_no_config_names_is_dropped(self):
        self.stage_area(f"{ORG}.kenya")
        self.advertise(f"{ORG}.ecmwf-ifs")

        run("cleanup_forti_orphans", apply=True)

        self.assertNotIn(f"{ORG}.kenya/178925760000/complete.json", self.keys())
        self.assertNotIn(f"latest/{ORG}.kenya", self.keys())

    def test_the_manifest_goes_too(self):
        """``*/complete.json`` is a completion marker.

        ``delete_prefix`` leaves markers alone by default — a reader may be
        following one — so a version directory that cannot lose its manifest
        never goes away, and the orphan survives every pass looking deleted.
        """
        self.stage_area(f"{ORG}.kenya")
        self.advertise(f"{ORG}.ecmwf-ifs")

        run("cleanup_forti_orphans", apply=True)

        self.assertEqual(self.keys(), {config.RAWDATAFORECASTER_PATH})

    def test_a_preview_deletes_nothing(self):
        self.stage_area(f"{ORG}.kenya")
        self.advertise(f"{ORG}.ecmwf-ifs")
        before = self.keys()

        output = run("cleanup_forti_orphans")

        self.assertEqual(self.keys(), before)
        self.assertIn(f"{ORG}.kenya", output)

    def test_an_area_the_bucket_config_still_advertises_is_kept(self):
        """The ordering rule, made structural.

        Between the rename and the republish the database has forgotten
        ``central.kenya`` and the config document on the bucket has not — because
        ``config.documents()`` withholds an empty ``areas`` list rather than
        writing one. Those bytes are what ``rawdataforecaster`` is serving from
        right now. Deleting them here is the gap the ordering exists to avoid,
        and the operator cannot see it coming.
        """
        self.stage_area(f"{ORG}.kenya")
        self.advertise(f"{ORG}.kenya")

        output = run("cleanup_forti_orphans", apply=True)

        self.assertIn(f"latest/{ORG}.kenya", self.keys())
        self.assertIn("still advertised", output)

    def test_an_area_a_row_claims_is_kept(self):
        self.stage_area(f"{ORG}.ecmwf-ifs")
        self.advertise(f"{ORG}.ecmwf-ifs")

        run("cleanup_forti_orphans", apply=True)

        self.assertIn(f"latest/{ORG}.ecmwf-ifs", self.keys())

    def test_a_disabled_rows_area_is_not_an_orphan(self):
        """Disabling is an operator switch, not a deletion.

        A disabled publication drops out of every serving query and out of the
        config — so by the config test alone its bytes look free. The row still
        owns the name, and re-enabling it must not need a republish.
        """
        FortiPublication.objects.filter(pk=self.publication.pk).update(is_enabled=False)
        self.stage_area(f"{ORG}.ecmwf-ifs")
        self.advertise(f"{ORG}.other")

        run("cleanup_forti_orphans", apply=True)

        self.assertIn(f"latest/{ORG}.ecmwf-ifs", self.keys())

    def test_the_documents_beside_the_areas_are_never_areas(self):
        """``config``, ``status`` and ``latest`` share the root with the areas.

        They are told apart by the dot every area key contains and none of them
        has. A pass that read them as areas would delete this instance's config
        the first time it ran.
        """
        self.sink.write(config.RAWDATAFORECASTER_PATH, config.encode({"areas": [f"{ORG}.ecmwf-ifs"]}))
        self.sink.write(config.JSONFORMAT_PATH, config.encode({"parameters": {}}))
        self.sink.write("status/sidecar.json", b"{}")

        run("cleanup_forti_orphans", apply=True)

        self.assertEqual(
            self.keys(),
            {config.RAWDATAFORECASTER_PATH, config.JSONFORMAT_PATH, "status/sidecar.json"},
        )

    def test_a_pointer_with_nothing_under_it_is_an_orphan(self):
        """``latest/<key>`` outlives its data if a prune ever ran out of order.

        It is the worst object on the bucket to leave behind: it is the load
        trigger, so a reader follows it to a version directory that is not there
        and fails at startup with an error that does not name the area.
        """
        self.sink.publish_markers([CompletionMarker(f"latest/{ORG}.kenya", b"178925760000")])
        self.advertise(f"{ORG}.ecmwf-ifs")

        run("cleanup_forti_orphans", apply=True)

        self.assertNotIn(f"latest/{ORG}.kenya", self.keys())

    def test_an_unreadable_config_stops_the_pass(self):
        """No config is not the same as a config naming nothing.

        The refusal above is only as good as the document it reads. A pass that
        treated an unparseable ``rawdataforecaster.json`` as "advertises nothing"
        would delete every area on the bucket at exactly the moment the instance
        could not tell it not to.
        """
        self.stage_area(f"{ORG}.kenya")
        self.sink.write(config.RAWDATAFORECASTER_PATH, b"not json")

        with self.assertRaises(CommandError):
            run("cleanup_forti_orphans", apply=True)

        self.assertIn(f"latest/{ORG}.kenya", self.keys())

    def test_an_absent_config_stops_the_pass_too(self):
        """Absence is the reading that looks safe and is not.

        Usually it means nothing was ever published here — and it equally
        describes a document somebody deleted by hand while the pair serves on
        from the copy already in its volume. From here the two are identical, and
        one of them makes this a deletion of the bytes answering every request.
        The cost of refusing is a message; the cost of proceeding is unrecoverable.
        """
        self.stage_area(f"{ORG}.kenya")

        with self.assertRaises(CommandError) as raised:
            run("cleanup_forti_orphans", apply=True)

        self.assertIn("not there", str(raised.exception))
        self.assertIn(f"latest/{ORG}.kenya", self.keys())
