"""The admin's Forti surfaces, and who may read each.

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

The **inspect page** is the third surface and the narrowest: one publication's
own readiness, mapping, last build and history, drawn from rows that carry its
foreign key and nothing else. It reads the serving plane not at all, which is a
property worth asserting rather than assuming — the two surfaces above both do,
and a read added to this page would put object storage's deadline on every look
at every publication. What is asserted here is the rendering and the audience;
the distinctions the rendering depends on are :mod:`~.tests.test_history`'s and
:mod:`~.tests.test_readiness`'s, on the data.
"""

import time
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from georiva.organisations.testing import dial_org
from georiva_publisher_forti.models import FortiPublication, FortiPublicationBuildLog, instance_sink

from .factories import make_collection, make_org_admin, make_publication, make_run, make_user
from .sink_isolation import TemporarySinkMixin
from .status_documents import write_forecaster

INSPECT_URL = "wagtailsnippets_georiva_publisher_forti_fortipublication:inspect"
EDIT_URL = "wagtailsnippets_georiva_publisher_forti_fortipublication:edit"

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


class PublicationHistoryTests(TestCase):
    """The history on one publication's inspect page, for the operator who owns it.

    No sink mixin and no status document anywhere in this class: the page reads
    the database and nothing else, and one of the tests below is what keeps it
    that way. A page that made a remote read would hold a worker on object
    storage's deadline every time somebody opened a publication to see how it
    was doing.
    """

    def setUp(self):
        dial_org(self.client)
        self.publication = make_publication(make_collection(), slug="ecmwf-ifs")
        self.url = reverse(INSPECT_URL, args=[self.publication.pk])

    def record(self, kind=None, outcome=None, publication=None, **fields):
        log = FortiPublicationBuildLog
        return log.record(
            publication or self.publication,
            kind or log.Kind.BUILD,
            outcome or log.Outcome.SUCCESS,
            timezone.now(),
            **fields,
        )

    def sign_in(self, user=None):
        self.client.force_login(user or make_org_admin("org-admin"))

    def test_an_organisation_administrator_sees_what_a_publish_did(self):
        """The figures, in the terms they were recorded in. No superuser here:
        the audience is the operator who runs the model, and the instance-wide
        panel turns them away."""
        self.record(version=PUBLISHED, step_count=15, point_count=1920, parameter_count=15, objects_written=4)
        self.sign_in()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(PUBLISHED))
        self.assertContains(response, "objects written")
        self.assertContains(response, "1920")

    def test_a_failed_attempt_shows_its_error_beside_the_attempt(self):
        """The whole of the feature's point for the operator having a bad day:
        the publication itself holds only the latest error, overwritten in
        place, so without this the third failure of the week is indistinguishable
        from the first."""
        self.record(outcome=FortiPublicationBuildLog.Outcome.FAILURE, error="GridMoved: 1920 points, pinned at 480")
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "GridMoved: 1920 points, pinned at 480")

    def test_a_failure_that_established_nothing_is_not_a_blank_row(self):
        """The template's one decision: what goes in the cell when there are no
        figures. A failure this early has none, and `str()` of a bare exception
        can be empty — so without the fallback this row is blank space where the
        error belongs."""
        self.record(outcome=FortiPublicationBuildLog.Outcome.FAILURE, error="")
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "Failed without a message")

    def test_a_retention_pass_is_distinguishable_from_a_publish(self):
        """They share a table and almost no figures, so the word that tells them
        apart has to be on the row."""
        self.record(kind=FortiPublicationBuildLog.Kind.GC, versions_pruned=3)
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "Clean-up")
        self.assertContains(response, "old versions removed")

    def test_a_publication_with_no_history_says_why_rather_than_rendering_nothing(self):
        """The state every publication is in until its first sweep, and
        therefore the first state this page is ever seen in. An empty region
        under a heading reads as a page that failed to load."""
        self.sign_in()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing has been published yet")

    def test_another_organisations_history_is_not_reachable(self):
        """The narrowing is the snippet view's, which scopes every single-object
        view to the active organisation — so this asserts that the page carrying
        a history obeys it, not that the history filters on its own."""
        theirs = make_publication(make_collection(slug="gfs-surface", org_slug="other-org"), slug="gfs")
        self.record(
            publication=theirs,
            outcome=FortiPublicationBuildLog.Outcome.FAILURE,
            error="not this organisation's failure",
        )
        self.sign_in()

        response = self.client.get(reverse(INSPECT_URL, args=[theirs.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertNotContains(response, "not this organisation's failure", status_code=404)

    def test_the_edit_form_carries_no_history(self):
        """The form is the decisions. An operator who opens it to correct a
        bbox is not asking what the publication has been doing, and the answer
        is one click away on the page that exists to give it."""
        self.record(outcome=FortiPublicationBuildLog.Outcome.FAILURE, error="GridMoved: 1920 points, pinned at 480")
        self.sign_in()

        response = self.client.get(reverse(EDIT_URL, args=[self.publication.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "GridMoved: 1920 points, pinned at 480")
        self.assertNotContains(response, "Nothing has been published yet")

    def test_rendering_a_publication_does_not_touch_object_storage(self):
        """Every other Forti surface reads the serving plane; this one must not.
        A page behind a storage deadline is a page that hangs for minutes when
        the bucket does, for an operator who only wanted to know whether their
        model was ready.
        """
        self.record(version=PUBLISHED, step_count=15, point_count=1920, parameter_count=15, objects_written=4)
        self.sign_in()
        sink = instance_sink()

        with (
            patch.object(type(sink), "exists", wraps=sink.exists) as exists,
            patch.object(type(sink), "read_json", wraps=sink.read_json) as read_json,
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(exists.call_count, 0)
        self.assertEqual(read_json.call_count, 0)


class PublicationReadinessTests(TestCase):
    """Readiness on the inspect page, asserted thinly and once.

    Every distinction this section draws belongs to :mod:`~.tests.test_readiness`
    and is tested there, on the data, without rendering anything. What is left
    for this class is the three things only a page can be wrong about: that the
    section is on the page an operator reads a publication from, that it
    carries the module's own words rather than a second set composed in a
    template, and that it is absent from the form, which is the decisions and
    not a diagnosis of them.

    No sink mixin and no status document, for the reason
    :class:`PublicationHistoryTests` gives: this reads the database and nothing
    else, and the inspect page's one remote-read assertion already covers the
    whole page it is part of.
    """

    def setUp(self):
        dial_org(self.client)
        self.publication = make_publication(make_collection(), slug="ecmwf-ifs")
        self.url = reverse(INSPECT_URL, args=[self.publication.pk])
        self.client.force_login(make_org_admin("org-admin"))

    def test_a_publication_waiting_for_its_first_run_says_so(self):
        """The state every publication is born in. An operator who reads "not
        ready yet" here waits, which is the correct action and the one no
        surface offered before."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "not ready yet")
        self.assertContains(response, "No complete forecast run")

    def test_a_blank_slot_reads_as_a_change_rather_than_a_wait(self):
        """The distinction, on the page. Both halves are here at once: the run
        has closed, so waiting is finished, and the mapping has a hole nothing
        but an operator will fill."""
        make_run(self.publication.collection)
        self.publication.variable_mappings.filter(slot="2t").update(variable=None)

        response = self.client.get(self.url)

        self.assertContains(response, "needs a change")
        self.assertContains(response, "2t is not set")

    def test_the_form_carries_no_readiness(self):
        """Neither the add form — there is no publication to be ready — nor
        the edit form, where a verdict above the bbox fields was a diagnosis
        in the way of a correction."""
        for url in (
            reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:add"),
            reverse(EDIT_URL, args=[self.publication.pk]),
        ):
            response = self.client.get(url)

            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "not ready yet")


class InspectPageTests(TestCase):
    """The publication's home, and the two things it offers to do.

    What is asserted is that the page carries each section in the words its
    module decided, that the mapping is readable here without being editable,
    and that the two actions — Mapping and Queue rebuild — are on it for the
    operator who may change the publication and not for one who may only look.
    """

    def setUp(self):
        dial_org(self.client)
        self.publication = make_publication(
            make_collection(),
            slug="ecmwf-ifs",
            published_version=PUBLISHED,
            point_count=1920,
            published_step_count=15,
            published_parameters=["air_temperature_2m"],
        )
        self.url = reverse(INSPECT_URL, args=[self.publication.pk])

    def sign_in(self, user=None):
        self.client.force_login(user or make_org_admin("org-admin"))

    def test_the_mapping_is_read_here_without_choosers(self):
        self.publication.variable_mappings.filter(slot="tcc").update(variable=None)
        self.sign_in()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<code>2t</code>", html=True)
        self.assertContains(response, "not set")
        self.assertNotContains(response, "<select")

    def test_what_was_last_published_is_on_the_page(self):
        """Fields the form never offered — they are output, not decisions — and
        that until now no page rendered at all."""
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, "Published version")
        self.assertContains(response, str(PUBLISHED))
        self.assertContains(response, "1920")

    def test_the_consumer_url_is_written_the_way_a_consumer_asks_it(self):
        self.sign_in()

        self.assertContains(self.client.get(self.url), "/api/forecast/ecmwf-ifs/")

    def test_an_operator_who_may_change_it_is_offered_mapping_and_rebuild(self):
        self.sign_in()

        response = self.client.get(self.url)

        self.assertContains(response, reverse("forti_publication_mapping", args=[self.publication.pk]))
        self.assertContains(response, reverse("forti_publication_queue_rebuild", args=[self.publication.pk]))

    def test_the_listing_offers_mapping_on_every_row(self):
        self.sign_in()

        response = self.client.get(reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:list"))

        self.assertContains(response, reverse("forti_publication_mapping", args=[self.publication.pk]))
        self.assertContains(response, self.url)

    def test_another_organisations_publication_is_not_inspectable(self):
        theirs = make_publication(make_collection(slug="gfs-surface", org_slug="other-org"), slug="gfs")
        self.sign_in()

        self.assertEqual(self.client.get(reverse(INSPECT_URL, args=[theirs.pk])).status_code, 404)


class QueueRebuildTests(TestCase):
    """The one action worth a button, and the three things a button must obey.

    It is a POST, because a GET that changed state is one a link preview could
    trigger. It is narrowed to the organisation, because every pk-taking page
    is. And it returns to the inspect page, so the status it flipped is read
    where it shows.
    """

    def setUp(self):
        dial_org(self.client)
        self.publication = make_publication(make_collection(), slug="ecmwf-ifs")
        self.url = reverse("forti_publication_queue_rebuild", args=[self.publication.pk])
        self.client.force_login(make_org_admin("org-admin"))

    def test_a_post_queues_and_returns_to_the_inspect_page(self):
        FortiPublication.objects.filter(pk=self.publication.pk).update(status=FortiPublication.Status.READY)

        response = self.client.post(self.url)

        self.assertRedirects(response, reverse(INSPECT_URL, args=[self.publication.pk]), fetch_redirect_response=False)
        self.publication.refresh_from_db()
        self.assertNotEqual(self.publication.status, FortiPublication.Status.READY)

    def test_a_get_changes_nothing(self):
        FortiPublication.objects.filter(pk=self.publication.pk).update(status=FortiPublication.Status.READY)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 405)
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, FortiPublication.Status.READY)

    def test_another_organisations_publication_cannot_be_queued(self):
        theirs = make_publication(make_collection(slug="gfs-surface", org_slug="other-org"), slug="gfs")
        FortiPublication.objects.filter(pk=theirs.pk).update(status=FortiPublication.Status.READY)

        response = self.client.post(reverse("forti_publication_queue_rebuild", args=[theirs.pk]))

        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, FortiPublication.Status.READY)
