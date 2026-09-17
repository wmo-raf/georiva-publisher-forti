"""The two surfaces that read the serving plane, and who may read each.

One document, two audiences, and the difference between them is the subject of
this module. The **panel** renders every figure the instance has:
``rawdataforecaster.json`` names every organisation's area keys, one sha
describes one document governing every tenant, and the two status files describe
one process serving all of them. A page rendered inside one organisation's admin
that showed all of it would hand ``ke-kmd.ecmwf-ifs`` to another organisation's
administrator — which is precisely what M5.6 spent D18 making impossible on the
public plane. So the gate is ``is_superuser``, checked in the view and not only
in the menu, and these tests are what stop it from quietly becoming "any admin".

The **publications listing** reads the same document and renders one row of it
per publication, for the organisation that owns that publication. It can do that
because an area row narrows and a configuration digest does not, and the tests
below are where that claim is held to: the narrowing, the single shared read,
and the distinctions a one-cell rendering could most easily flatten.

They live together because they are one decision seen from both sides. Split
apart, the listing's tests would read as a listing feature rather than as the
repair the panel's access rule made necessary.
"""

import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from georiva.organisations.testing import dial_org
from georiva_publisher_forti.models import FortiPublication

from .factories import make_collection, make_org_admin, make_publication, make_user
from .sink_isolation import TemporarySinkMixin
from .status_documents import write_forecaster

PUBLISHED = 178835040000

#: One run older, so a reader holding it disagrees with what the database says
#: was published — the state the column exists to make visible.
BEHIND = 178813440000


class PanelAccessTests(TemporarySinkMixin, TestCase):
    def setUp(self):
        self.isolate_sink()
        dial_org(self.client)
        self.url = reverse("forti_verification_panel")

    def test_the_instance_admin_may_open_it(self):
        self.client.force_login(make_user("instance-admin", superuser=True))

        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_an_organisation_administrator_may_not(self):
        """Wagtail already gates every ``register_admin_urls`` pattern behind
        ``require_admin_access``, and admin access is what an organisation's own
        editors have — so the instance-admin condition has to be its own check."""
        self.client.force_login(make_user("editor", superuser=False))

        self.assertNotEqual(self.client.get(self.url).status_code, 200)


class PanelRenderTests(TemporarySinkMixin, TestCase):
    """It is developed in the state where nothing has been published and the
    pair has never run, which is also the most likely first real state. "Not cut
    over yet" has to render legibly rather than as four failures — or as a
    traceback, which is what a template that assumed a status document would do.
    """

    def setUp(self):
        self.isolate_sink()
        dial_org(self.client)
        self.publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["air_temperature_2m"],
        )
        self.client.force_login(make_user("instance-admin", superuser=True))
        self.url = reverse("forti_verification_panel")

    def test_an_instance_that_has_not_cut_over_renders(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "rawdataforecaster.json")
        self.assertContains(response, "jsonformat.json")

    def test_the_empty_state_says_not_yet_rather_than_could_not_read(self):
        """Both are "no sha" from here, and only one of them is somebody's
        afternoon. The bucket is reachable in this test and empty."""
        response = self.client.get(self.url)

        self.assertContains(response, "not yet")
        self.assertNotContains(response, "could not read")

    def test_every_organisation_area_key_is_on_the_one_page(self):
        """The leak the access rule closes, demonstrated rather than asserted in
        prose: one process serves every tenant, so its status file names every
        tenant's areas and there is no filter that could narrow them."""
        import json

        other = make_publication(
            make_collection(slug="gfs-surface", org_slug="other-org"),
            slug="gfs",
            published_version=PUBLISHED,
            point_count=64,
            published_step_count=4,
            published_parameters=["air_temperature_2m"],
        )
        from georiva_publisher_forti import verification
        from georiva_publisher_forti.models import instance_sink

        instance_sink().write(
            verification.RAWDATAFORECASTER_STATUS_PATH,
            json.dumps(
                {
                    "module": "rawdataforecaster",
                    "loaded_sha": "f" * 64,
                    "loaded_at": "2026-09-13T09:00:00Z",
                    "ok": True,
                    "state": {
                        "areas": [
                            {"area": self.publication.area_key, "available": PUBLISHED, "loaded": PUBLISHED},
                            {"area": other.area_key, "available": PUBLISHED, "loaded": PUBLISHED},
                        ]
                    },
                }
            ).encode("utf-8"),
        )

        response = self.client.get(self.url)

        self.assertContains(response, self.publication.area_key)
        self.assertContains(response, other.area_key)

    def test_nothing_on_the_page_offers_to_change_anything(self):
        """Read-only is the decision (D23): a write here would be reverted by the
        reconciler within 60 s while still showing what somebody typed."""
        body = self.client.get(self.url).content.decode()

        self.assertNotIn("<form", body.lower())


class PublicationIndexResidencyTests(TemporarySinkMixin, TestCase):
    """The repair the panel's docstring names, on the surface it names.

    An area row narrows safely to one organisation and a configuration digest
    does not, which is the whole reason this can be an organisation
    administrator's column while the page above stays the instance admin's. The
    distinctions have to survive the narrowing: a document that could not be
    read is not a document that is not there, and a listing that collapsed them
    would report an outage as an instance that has not cut over.
    """

    def setUp(self):
        self.isolate_sink()
        dial_org(self.client)
        self.publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["air_temperature_2m"],
        )
        self.area = self.publication.area_key
        self.url = reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:list")

    def sign_in(self, user=None):
        self.client.force_login(user or make_org_admin("org-admin"))

    def test_the_resident_version_is_shown_beside_the_published_one(self):
        """Asserted on the resident cell's own markup, not on the digits.

        When the two agree they are the same digits, so asserting the number is
        merely *present* is an assertion the ``published_version`` column
        satisfies on its own: the test passed while proving only that a column
        the page already had still rendered. Counting occurrences does not fix
        it either — the version is in the cell's ``title`` sentences as well, so
        the count tracks the tooltip wording rather than the figure.

        The figure is the one place the version renders as a span's whole text
        (``residency_cell.html``); the published column renders it bare in its
        ``<td>``. That is what makes this an assertion about *this* cell.
        """
        write_forecaster(areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])
        self.sign_in()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f">{PUBLISHED}</span>", count=1)
        self.assertContains(response, "agrees")

    def test_a_resident_version_behind_the_published_one_is_told_apart_from_agreement(self):
        """Published and resident become two facts rather than one assumed to
        imply the other, which is the whole point of the column."""
        write_forecaster(areas=[{"area": self.area, "available": BEHIND, "loaded": BEHIND}])
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "differs")
        self.assertNotContains(response, "agrees")
        # The two figures differ here, so each can be asserted on its own: the
        # resident version renders in the cell, and the published one it
        # disagrees with is still in the row beside it.
        self.assertContains(response, f">{BEHIND}</span>")
        self.assertContains(response, str(PUBLISHED))

    def test_an_organisation_administrator_sees_it_for_their_own_publications(self):
        """No superuser anywhere in this test. The audience is the operator who
        runs one organisation's models, and the panel above turns them away."""
        write_forecaster(areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])
        self.sign_in()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "agrees")
        self.assertNotEqual(self.client.get(reverse("forti_verification_panel")).status_code, 200)

    def test_another_organisations_residency_does_not_reach_this_listing(self):
        """The status document names every organisation's areas — one process
        serves all of them — so what is read is instance-wide and only what is
        *rendered* narrows. The other organisation's area is given a version of
        its own, so a leak would be a number on the page rather than a row
        somebody has to notice is extra.
        """
        theirs = make_publication(
            make_collection(slug="gfs-surface", org_slug="other-org"),
            slug="gfs",
            published_version=BEHIND,
        )
        write_forecaster(
            areas=[
                {"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED},
                {"area": theirs.area_key, "available": BEHIND, "loaded": BEHIND},
            ]
        )
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, str(PUBLISHED))
        self.assertNotContains(response, str(BEHIND))
        self.assertNotContains(response, theirs.slug)

    def test_an_unreadable_status_document_says_so_rather_than_nothing_yet(self):
        """Collapsing these reports an outage as an instance that has not cut
        over — which is exactly the state this instance is in, so the conflation
        would be invisible for as long as it mattered."""
        from georiva_publisher_forti.models import instance_sink

        self.sign_in()
        sink = instance_sink()

        with patch.object(type(sink), "exists", side_effect=OSError("connection refused")):
            response = self.client.get(self.url)

        self.assertContains(response, "could not read")
        self.assertNotContains(response, "not yet")
        self.assertNotContains(response, "no reader")

    def test_a_publication_that_has_never_published_is_nothing_yet(self):
        self.publication.published_version = None
        self.publication.save(update_fields=["published_version"])
        write_forecaster(areas=[])
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "not yet")
        self.assertNotContains(response, "could not read")

    def test_every_row_is_served_by_one_status_read(self):
        """The document lists every area at once. A read per row would put the
        deadline on the page rather than on the read, and a listing of twenty
        models would be twenty round trips to object storage."""
        from georiva_publisher_forti.models import instance_sink

        for n in range(4):
            make_publication(make_collection(slug=f"model-{n}"), slug=f"m{n}", published_version=PUBLISHED)
        write_forecaster(areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])
        self.sign_in()
        sink = instance_sink()

        with patch.object(type(sink), "read_json", wraps=sink.read_json) as read_json:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_json.call_count, 1)

    def test_the_listing_renders_when_object_storage_does_not_answer(self):
        """One worker thread, one deadline, abandoned rather than waited on. The
        alternative is an admin page holding a worker through botocore's retry
        ladder, which is minutes, five times over."""
        from georiva_publisher_forti import verification

        def slow(sink, path, module):
            time.sleep(3.0)

        self.sign_in()
        began = time.monotonic()
        with (
            patch.object(verification, "read_status", slow),
            override_settings(GEORIVA_FORTI_VERIFICATION_DEADLINE=0.1),
        ):
            response = self.client.get(self.url)
        elapsed = time.monotonic() - began

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not read")
        self.assertLess(elapsed, 2.0)

    def test_a_reader_that_has_never_reported_is_not_a_publication_that_has_not(self):
        """Nothing written under ``status/`` at all. The publication *has*
        published, so "not yet" would point the operator at their own model for
        a process that is not running."""
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "no reader")
        self.assertNotContains(response, "could not read")

    def test_the_results_partial_carries_the_column_too(self):
        """Wagtail builds ``results/`` from the same ``index_view_class``
        (`viewsets/model.py:259`), which is what makes search, filtering and
        paging keep the column and keep it to one read. That is an internal a
        version bump could move, and every other test here goes through the full
        page — so the partial is asserted on its own."""
        write_forecaster(areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])
        self.sign_in()

        response = self.client.get(f"{self.url}results/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "agrees")
        self.assertContains(response, str(PUBLISHED))

    def test_a_listing_with_no_rows_does_not_touch_object_storage(self):
        """The reading is made when the first cell asks and not before, so a
        fresh organisation renders its empty table without a round trip."""
        from georiva_publisher_forti.models import instance_sink

        FortiPublication.objects.all().delete()
        self.sign_in()
        sink = instance_sink()

        with patch.object(type(sink), "exists", wraps=sink.exists) as exists:
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(exists.call_count, 0)

    def test_the_reading_does_not_outlive_the_request_that_made_it(self):
        """The trap the column is shaped around. The reading is cached on the
        column and the column is built per request — declared in ``list_display``
        it would be built once at import, and every request for the life of the
        process would be answered with the first one's reading."""
        write_forecaster(areas=[{"area": self.area, "available": BEHIND, "loaded": BEHIND}])
        self.sign_in()

        self.assertContains(self.client.get(self.url), "differs")
        write_forecaster(areas=[{"area": self.area, "available": PUBLISHED, "loaded": PUBLISHED}])

        self.assertContains(self.client.get(self.url), "agrees")
