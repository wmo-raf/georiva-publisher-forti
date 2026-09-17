"""The operator's surfaces: one publication, a list of them, and the serving plane.

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

What that gave up, explicitly, was that an organisation administrator could not
see whether their own model is resident and at which version. That is repaired
where it was always going to be — beside the publication, whose organisation
*is* known — by :class:`ResidencyColumn` on the listing, and **not** by widening
this page's audience to the documents it cannot narrow. An area row is one key,
one version and one organisation; a configuration sha describes one document
governing every tenant and narrows to nobody. That difference is the whole of
why one of these is an organisation's and the other is not.

The last surface is :class:`PublishHistoryPanel`, and it is the only one that
asks nothing of the serving plane: what a publication has *attempted* is a
question the database answers on its own. It therefore sits where an operator
already is when they ask it — on the publication's own page — and inherits that
page's narrowing rather than declaring a second one.
"""

import logging

from django.contrib import messages
from django.forms.models import ModelChoiceIterator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse, reverse_lazy
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from wagtail import hooks
from wagtail.admin.auth import permission_denied
from wagtail.admin.menu import MenuItem
from wagtail.admin.panels import FieldPanel, MultiFieldPanel, Panel
from wagtail.admin.ui.tables import Column
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import CreateView, EditView, IndexView, SnippetViewSet

from georiva.organisations.scoping import OrgScopedViewSetMixin

from . import history, verification
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


class ResidencyColumn(Column):
    """What is actually resident, beside what the database says was published.

    The two are different facts, and the listing showed only the second — so an
    operator read "published, version N" and had nothing to tell them whether
    any process was serving it. The one surface that knew was the instance-wide
    panel, which an organisation administrator may not open.

    **One read for the whole page.** ``rawdataforecaster``'s status document
    lists every area at once, so the reading is a ``cached_property`` on the
    column and the column is built per request: the first cell pays for the
    read and every other cell is a dictionary lookup. A column that read per row
    would put object storage's deadline on the *page* rather than on the read,
    and a listing of twenty models would be twenty round trips.

    **Narrowed by construction.** :meth:`~.verification.ResidentAreas.of` takes
    the publication whose row is being rendered, so a cell can only ever ask
    about the area it already holds — there is no call shape here that returns
    somebody else's row. Why that is safe on a page the panel is not is argued
    once, in this module's docstring.
    """

    cell_template_name = "georiva_publisher_forti/tables/residency_cell.html"

    @cached_property
    def resident(self):
        """The one read, made when the first cell asks and not before.

        Lazy rather than eager in ``__init__`` so a listing with no rows — a
        fresh organisation, or a search that matched nothing — does not touch
        object storage to render an empty table.
        """
        return verification.resident_areas()

    def get_value(self, instance):
        return self.resident.of(instance)


class PublishHistoryPanel(Panel):
    """Every attempt this publication has made, on the publication's own page.

    The rows have been recorded since the plugin's first publish and nothing has
    ever rendered one. The publication holds only the *latest* state — a failed
    build overwrites the previous error in place — so an operator could see that
    the last publish failed and could not tell a first failure from a week of
    them.

    **A panel on the edit page, not a page of its own.** The edit page is where
    an operator already is when they ask what a publication has been doing, and
    it is already narrowed to their organisation: ``OrgScopedViewSetMixin``
    scopes every single-object view, so a foreign pk is a 404 before this panel
    is built. A separate route would be a second place that narrowing has to be
    remembered, which is the arrangement in which one of the two is later
    forgotten.

    **It reads the database and nothing else.** Every other Forti surface reads
    the serving plane, and putting object storage's deadline behind an edit form
    would mean a bbox could not be corrected while the bucket was slow. The
    question this panel answers — what has this publication done — is answerable
    without asking any remote process, so it asks none.
    """

    class BoundPanel(Panel.BoundPanel):
        template_name = "georiva_publisher_forti/panels/publish_history.html"

        def is_shown(self):
            """Hidden on the add form, where there is no publication to have a
            history: a reverse relation on an unsaved instance raises rather
            than coming back empty, and an empty history section above a form
            that has never been saved would answer a question nobody asked."""
            return bool(self.instance and self.instance.pk)

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            context["history"] = history.report(self.instance)
            return context


class FortiPublicationIndexView(IndexView):
    """The listing, with residency spliced in beside the published version.

    The column is built here rather than declared in ``list_display`` because it
    holds a per-request reading: a column instance on the viewset would be built
    once at import and would then serve the first request's answer to every
    request after it, for the life of the process.
    """

    #: The column whose answer this one qualifies. Beside it rather than at the
    #: end, because the pair is the point — "published N, resident N" is one
    #: fact read across two cells, and a column between them would break it.
    RESIDENCY_AFTER = "published_version"

    def get_base_queryset(self):
        """``area_key`` reaches through the catalog to the organisation, and a
        listing page asks every row for one. Without this the column is a query
        per row — which would undo, in the database, exactly what the single
        status read buys on the network."""
        return super().get_base_queryset().select_related("collection__catalog__organisation")

    @cached_property
    def columns(self):
        columns = list(super().columns)
        residency = ResidencyColumn("resident", label=_("Resident"))
        after = next(
            (index for index, column in enumerate(columns) if column.name == self.RESIDENCY_AFTER),
            len(columns) - 1,
        )
        columns.insert(after + 1, residency)
        return columns


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
    index_view_class = FortiPublicationIndexView
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
        ),
        FieldPanel("status", read_only=True),
        FieldPanel("error", read_only=True),
        PublishHistoryPanel(heading="History", icon="history"),
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
