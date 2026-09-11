"""The operator's view of a publication.

Almost everything on the model is *output* — the pinned grid, what was last
published, the lock bookkeeping — so the form offers only the handful of fields
that are genuinely decisions: which collection, what the model is called, who may
ask for it, how far it reaches, and whether it is on.

The one action worth a button is a re-queue. It goes through ``queue_rebuild``
rather than dispatching, so it shares the sweep's locking exactly and cannot take
a publication out from under a worker that is mid-write.
"""

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from wagtail import hooks
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import SnippetViewSet

from georiva.organisations.scoping import OrgScopedViewSetMixin

from .models import FortiPublication

logger = logging.getLogger(__name__)


class FortiPublicationViewSet(OrgScopedViewSetMixin, SnippetViewSet):
    model = FortiPublication
    icon = "site"
    menu_label = "Forti publications"
    list_display = ["slug", "collection", "visibility", "status", "published_version", "built_at"]
    list_filter = ["status", "visibility", "is_enabled"]
    panels = [
        MultiFieldPanel(
            [
                FieldPanel("collection"),
                FieldPanel("slug"),
                FieldPanel("visibility"),
                FieldPanel("is_enabled"),
            ],
            heading="What is published",
            help_text=(
                "The slug is the name a consumer asks by — GET /api/forecast/"
                "{slug}/ — and a segment of every storage key, so it is fixed "
                "once this model has published. Leave it and the visibility "
                "blank to take the catalog slug and the collection's own tier."
            ),
        ),
        MultiFieldPanel(
            [
                FieldPanel("west"),
                FieldPanel("south"),
                FieldPanel("east"),
                FieldPanel("north"),
            ],
            heading="Extent",
            help_text=(
                "The points that exist. Give it a margin past the area you care "
                "about: a border town is asked for from both sides, and an area "
                "that stops at the boundary answers 'outside coverage' to half of "
                "them. Changing this changes the point list, which is pinned — "
                "the next build refuses rather than republishing under a grid "
                "that moved."
            ),
        ),
        MultiFieldPanel(
            [
                FieldPanel("time_until_next_hours"),
            ],
            heading="Advanced",
        ),
        FieldPanel("status", read_only=True),
        FieldPanel("error", read_only=True),
    ]


register_snippet(FortiPublicationViewSet)


@hooks.register("register_admin_urls")
def register_forti_admin_urls():
    return [
        path(
            "forti/publication/<int:publication_pk>/queue-rebuild/",
            queue_rebuild,
            name="forti_publication_queue_rebuild",
        ),
    ]


def queue_rebuild(request, publication_pk):
    """Re-queue a publication for the next sweep.

    Deliberately not a dispatch. ``queue_rebuild`` is a single conditional UPDATE
    that refuses a publication a worker is actively holding, so an impatient
    operator cannot start a second writer on one area — and the sweep picks the
    row up on its normal cadence either way.
    """
    publication = get_object_or_404(FortiPublication, pk=publication_pk)

    if publication.queue_rebuild():
        messages.success(request, f"{publication.slug} will be republished by the next sweep.")
    else:
        messages.warning(
            request,
            f"{publication.slug} is being published right now — leaving it to the worker that holds it.",
        )

    return redirect(reverse("wagtailsnippets_georiva_publisher_forti_fortipublication:list"))
