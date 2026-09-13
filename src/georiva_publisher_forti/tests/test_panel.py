"""Who may open the panel, and that it renders in the state this instance is in.

The access rule is the sharp one and the reason is worth restating where it is
tested: every figure on the page is instance-wide. ``rawdataforecaster.json``
names every organisation's area keys, one sha describes one document governing
every tenant, and the two status files describe one process serving all of them.
A page rendered inside one organisation's admin that showed all of it would hand
``ke-kmd.ecmwf-ifs`` to another organisation's administrator — which is precisely
what M5.6 spent D18 making impossible on the public plane.

So the gate is ``is_superuser``, checked in the view and not only in the menu,
and these tests are what stop it from quietly becoming "any admin" later.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from georiva.organisations.testing import DEFAULT_TEST_ORG_SLUG, dial_org, join_org

from .factories import make_collection, make_publication
from .sink_isolation import TemporarySinkMixin

PUBLISHED = 178835040000


def make_user(username, *, superuser):
    user = get_user_model().objects.create_user(
        username=username,
        email=f"{username}@example.org",
        password="not-a-real-password",
        is_staff=True,
        is_superuser=superuser,
    )
    join_org(user, DEFAULT_TEST_ORG_SLUG)
    return user


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
