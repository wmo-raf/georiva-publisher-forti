"""The operator's two surfaces: one publication, and the whole serving plane.

Almost everything on the model is *output* — the pinned grid, what was last
published, the lock bookkeeping — so the form offers only the handful of fields
that are genuinely decisions: which collection, what the model is called, who may
ask for it, how far it reaches, and whether it is on.

The one decision the form *guards* is which collection: the chooser offers
forecast collections and nothing else, for the reason
:class:`ForecastCollectionsOnly` gives. Everything else that would stop a publish
is diagnosis rather than a filter, and is not this form's business.

The one action worth a button is a re-queue. It goes through ``queue_rebuild``
rather than dispatching, so it shares the sweep's locking exactly and cannot take
a publication out from under a worker that is mid-write.

The other surface is :func:`verification_panel`, which renders
:func:`~.verification.report` — the four hops a config document makes between
this database and the process serving from it (D23). Where it lives and who may
see it are the two decisions the plan left open, and both are settled here:

**A ``register_admin_urls`` view with a Settings menu item, not a snippet
view.** It is about neither one publication nor a list of them: three of its
four hops are facts about the instance's deployment, and two of its tables have
no publication in them at all. Hanging it off ``FortiPublicationViewSet`` would
have put an instance-wide page inside a per-organisation listing, which is
exactly the confusion the access rule below exists to prevent.

**Visible to the instance admin, and to nobody else.** Every figure on the page
is instance-wide — ``rawdataforecaster.json`` names *every* organisation's area
keys, one sha describes one document governing every tenant, and the two status
files describe one process serving all of them — so rendering it inside one
organisation's admin would hand ``ke-kmd.ecmwf-ifs`` to another organisation's
administrator. That is the leak; it is not the argument. The argument is the
audience: every action this page prompts — restart the pair, fix the endpoint,
re-fetch the compose file at the pinned tag — belongs to whoever deployed the
compose file, which is the instance admin. There is consequently nothing here to
narrow, and no ``scoped_queryset``: narrowing a sha is not a thing that can be
done, and a page showing half a chain would answer a question nobody asked.

What that gives up, explicitly: an organisation administrator cannot see whether
their own model is resident and at which version. That is a real loss and the
right place to repair it is beside the publication, whose organisation *is*
known — not by widening this page's audience to the documents it cannot narrow.
"""

import logging

from django.contrib import messages
from django.forms.models import ModelChoiceIterator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from wagtail import hooks
from wagtail.admin.auth import permission_denied
from wagtail.admin.menu import MenuItem
from wagtail.admin.panels import FieldPanel, MultiFieldPanel
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import CreateView, EditView, SnippetViewSet

from georiva.organisations.scoping import OrgScopedViewSetMixin

from . import verification
from .models import FortiPublication

logger = logging.getLogger(__name__)


class ForecastCollectionsOnly(ModelChoiceIterator):
    """The forecast collections of whatever queryset the field ends up with.

    The **offered** rows, not the **accepted** ones, and the distinction is the
    whole design. Narrowing the field's queryset would have been shorter and
    would have made the form answer a non-forecast id with "Select a valid
    choice. That choice is not one of the available choices." — because a field
    that rejects a value excludes it from model validation, so
    :meth:`FortiPublication.clean`'s sentence would never reach the page. Leaving
    the queryset alone keeps the two halves distinct: the dropdown is the hint,
    and the refusal an operator reads is the model's.

    It also keeps the *other* refusal honest. The same field is narrowed to the
    active organisation by ``scope_form_fields``, and a foreign id has to be
    turned away as absent — not told to tick a box on a collection that is
    already a forecast and simply is not theirs.

    The filter is applied per iterator rather than once, because the field builds
    a fresh one each time its queryset is assigned: ``scope_form_fields`` runs
    after this mixin and reassigns it, and the rebuilt iterator then filters the
    organisation's rows rather than the instance's.
    """

    def __init__(self, field):
        super().__init__(field)
        self.queryset = self.queryset.filter(is_forecast=True)


class ForecastCollectionsOnlyMixin:
    """Offers the collection chooser only what this plugin can actually publish.

    The widget half of the forecast rule; :meth:`FortiPublication.clean` is the
    other half, and neither is sufficient alone. A filtered dropdown is a hint an
    operator meets before choosing wrongly, and a posted id never goes near it;
    model validation is the rule, and an operator meets it only after filling in
    the whole form.
    """

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        field = form.fields["collection"]
        field.iterator = ForecastCollectionsOnly
        # The widget is still holding the iterator built with the field, so the
        # new class governs nothing until the choices are rebuilt through it.
        field.widget.choices = field.choices
        return form


class FortiPublicationCreateView(ForecastCollectionsOnlyMixin, CreateView):
    pass


class FortiPublicationEditView(ForecastCollectionsOnlyMixin, EditView):
    pass


class FortiPublicationViewSet(OrgScopedViewSetMixin, SnippetViewSet):
    model = FortiPublication
    icon = "site"
    menu_label = "Forti publications"
    list_display = ["slug", "collection", "visibility", "status", "published_version", "built_at"]
    list_filter = ["status", "visibility", "is_enabled"]
    add_view_class = FortiPublicationCreateView
    edit_view_class = FortiPublicationEditView
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
                FieldPanel("generation"),
            ],
            heading="Advanced",
            help_text=(
                "The generation is what makes a reader load a republish of a run "
                "it has already loaded. Raise it by one whenever something other "
                "than the run changes what this model should publish, then "
                "republish: without it the new bytes land under the stamp the "
                "reader already holds and are never read, and nothing reports "
                "that. It counts within one run and resets itself on the next."
            ),
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
        path(
            "forti/serving/",
            verification_panel,
            name="forti_verification_panel",
        ),
    ]


class SuperuserMenuItem(MenuItem):
    """Shown to the instance admin alone, matching core's own precedent.

    The menu is decoration and not the gate — :func:`verification_panel` checks
    the same thing itself — but the two read one condition so an organisation
    administrator never sees an entry that would turn them away.
    """

    def is_shown(self, request):
        return bool(request.user.is_superuser)


@hooks.register("register_settings_menu_item")
def register_forti_serving_menu_item():
    """Settings rather than the sidebar: this is how the instance is deployed,
    not data anybody browses. It sits beside Boundaries, which is gated the same
    way and for the same kind of reason."""
    return SuperuserMenuItem(
        _("Forti serving"),
        reverse_lazy("forti_verification_panel"),
        icon_name="site",
        order=130,
    )


def verification_panel(request):
    """Read-only, and that is the decision rather than the limitation (D23).

    GeoRiva's database is the single authority and ``refresh_forti_config``
    rewrites the bucket from it every five minutes, so a form here that wrote a
    config document would be reverted within one reconciler tick while still
    showing what somebody typed. There is no honest way to offer an edit whose
    effect expires in 60 seconds, so none is offered.

    Wagtail already gates every ``register_admin_urls`` pattern behind
    ``require_admin_access``; this adds the instance-admin condition on top,
    because admin access is what an organisation's editors have.
    """
    if not request.user.is_superuser:
        return permission_denied(request)

    context = {
        "breadcrumbs_items": [
            {"url": reverse("wagtailadmin_home"), "label": _("Home")},
            {"url": None, "label": _("Forti serving")},
        ],
        "header_title": _("Forti serving"),
        "header_icon": "site",
        "report": verification.report(),
    }
    return render(request, "georiva_publisher_forti/verification_panel.html", context)


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
