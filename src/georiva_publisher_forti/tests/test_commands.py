"""The operator commands, and the invariants they are allowed to move.

Both exist because of the same fact: the slug is a segment of every storage key,
so a rename is a storage operation wearing a database operation's clothes. The
model refuses it (``_published_under_another_slug``) and is right to. These
commands are how an operator performs one anyway — in the order that leaves
nothing serving from bytes nobody can find.

What is worth testing here is not that the commands do what they say. It is the
refusals, because each of them is the only thing standing between an operator and
a state nothing reports. ``rename_forti_model`` must leave the row
**unpublished**, not merely renamed: a row carrying ``published_version`` under a
name whose bytes are not on the bucket is advertised by ``rdfconfig.servable``
and 404s from ``rawdataforecaster`` — the exact state ``serving.py``'s 404 branch
was written to describe and cannot fix.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG
from georiva_publisher_forti.models import FortiPublication

from .factories import make_collection, make_publication

#: The organisation ``make_collection`` builds under. Every area key below is
#: derived from it rather than spelled, because the key is ``{org}.{slug}`` and
#: a test that hard-codes the organisation half is testing the fixture.
ORG = DEFAULT_TEST_ORG_SLUG


def run(command, *args, **options):
    out = StringIO()
    call_command(command, *args, stdout=out, stderr=out, **options)
    return out.getvalue()


class RenameTests(TestCase):
    def setUp(self):
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

    def test_a_preview_changes_nothing(self):
        output = run("rename_forti_model", "kenya", "ecmwf-ifs")

        self.assertEqual(self.reread().slug, "kenya")
        self.assertIn("--apply", output)

    def test_the_preview_names_the_bytes_it_is_about_to_orphan(self):
        """The whole cost of the operation, before it is paid.

        An operator who cannot see which keys stop being reachable has no way to
        know that the rename is the easy half.
        """
        output = run("rename_forti_model", "kenya", "ecmwf-ifs")

        self.assertIn(f"{ORG}.kenya", output)
        self.assertIn(f"{ORG}.ecmwf-ifs", output)

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
