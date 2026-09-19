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

The publication itself has three pages, and the split between them is the
difference between changing a thing and reading it:

**The form** is the decisions and nothing else. Everything the model *reports* —
its readiness, its status, what it last published, every attempt it has made —
used to be rendered on the edit page too, and an operator who opened it to
correct a bbox was reading a diagnosis first.

**The inspect page** is where that reporting went. It is the publication's home:
what it is, whether it would publish, what fills each slot, what it last wrote,
and everything it has attempted — in that order, which is the order the
questions arrive in. Every save of the form and of the mapping returns here, so
the consequence of a change is read where it shows. It reads the database and
nothing else, for the reason the history panel always gave: a page behind object
storage's deadline is one that cannot be opened while the bucket is slow.

**The mapping page** is the eight slots on a page of their own, reached from the
listing and from the inspect page, and the page a new publication lands on so
that what auto-match found is read by the one person who can judge it. Its rule
is in :mod:`~.forms`.

Every page that takes a pk narrows to the active organisation: the snippet views
through ``OrgScopedViewSetMixin``, the hand-written ones through
``get_org_object_or_404`` — so a foreign pk is a 404 before anything is built.
"""

import logging

from django.contrib import messages
from django.forms.models import ModelChoiceIterator
from django.shortcuts import redirect, render
from django.urls import path, reverse, reverse_lazy
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST
from wagtail import hooks
from wagtail.admin.auth import permission_denied
from wagtail.admin.menu import MenuItem
from wagtail.admin.panels import FieldPanel, MultiFieldPanel, ObjectList
from wagtail.admin.ui.menus import MenuItem as RowMenuItem
from wagtail.admin.ui.tables import Column
from wagtail.permissions import ModelPermissionPolicy
from wagtail.snippets.models import register_snippet
from wagtail.snippets.views.snippets import CreateView, EditView, IndexView, InspectView, SnippetViewSet

from georiva.core.menus import PUBLICATIONS_MENU_HOOK
from georiva.organisations.access import get_org_object_or_404
from georiva.organisations.scoping import OrgScopedViewSetMixin

from . import forms, history, readiness, verification
from .models import FortiPublication

SNIPPET = "wagtailsnippets_georiva_publisher_forti_fortipublication"

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


#: The fields the inspect page reads as the publication's identity, and the
#: ones it reads as its last build. Named here rather than left to Wagtail's
#: "every concrete field" default because that default would also list the lock
#: bookkeeping, which is nobody's question.
IDENTITY_FIELDS = (
    "collection",
    "slug",
    "visibility",
    "is_enabled",
    "west",
    "south",
    "east",
    "north",
    "time_until_next_hours",
    "generation",
)
LAST_BUILD_FIELDS = (
    "status",
    "built_at",
    "published_version",
    "published_reference_time",
    "published_step_count",
    "published_parameters",
    "point_count",
    "grid_id",
    "error",
)


def can_change(user) -> bool:
    """The one gate the mapping page and the re-queue share with the form.

    Wagtail's own ``change`` permission on the model, which is what the snippet
    edit view asks — so an operator who may edit the publication may map it and
    re-queue it, and one who may only look at it may do neither.
    """
    return ModelPermissionPolicy(FortiPublication).user_has_permission(user, "change")


class FortiPublicationInspectView(InspectView):
    """The publication's home: what it is, and everything it reports.

    Five sections in the order an operator's questions arrive — what is this,
    can it publish, what does it publish, what did it last publish, what has it
    been doing. The first and fourth are model fields rendered by Wagtail's own
    machinery; the other three are :mod:`~.readiness`, the mapping and
    :mod:`~.history`, each of which decides its words before the template sees
    them. Nothing here reads the serving plane; residency is the listing's.
    """

    template_name = "georiva_publisher_forti/inspect.html"
    fields = [*IDENTITY_FIELDS, *LAST_BUILD_FIELDS]

    def get_field_display_value(self, field_name, field):
        """Wagtail's own reading, with three things it renders badly read for
        it: an absent value prints as ``None``, a boolean as ``True`` and a JSON
        list as its Python repr."""
        value = super().get_field_display_value(field_name, field)
        if value is None or value == "":
            return "—"
        if value is True or value is False:
            return _("Yes") if value else _("No")
        if isinstance(value, list):
            return ", ".join(str(item) for item in value)
        return value

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        by_name = dict(zip(self.fields, context["fields"], strict=True))
        context["identity"] = [by_name[name] for name in IDENTITY_FIELDS]
        context["last_build"] = [by_name[name] for name in LAST_BUILD_FIELDS]
        context["readiness"] = readiness.report(self.object)
        context["mapping"] = forms.mapping_summary(self.object)
        context["history"] = history.report(self.object)
        context["mapping_url"] = reverse("forti_publication_mapping", args=[self.object.pk])
        context["queue_rebuild_url"] = reverse("forti_publication_queue_rebuild", args=[self.object.pk])
        context["can_change"] = can_change(self.request.user)
        context["incomplete_label"] = forms.INCOMPLETE_LABEL
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

    def get_list_more_buttons(self, instance):
        """Mapping beside Edit, on every row: it is the other half of
        configuring a publication, and the listing is where an operator goes
        to find the one they mean to configure."""
        buttons = super().get_list_more_buttons(instance)
        if can_change(self.request.user):
            buttons.append(
                RowMenuItem(
                    _("Variables"),
                    url=reverse("forti_publication_mapping", args=[instance.pk]),
                    icon_name="list-ul",
                    priority=15,
                )
            )
        return buttons


class FortiPublicationCreateView(ForecastCollectionsOnlyMixin, CreateView):
    def get_success_url(self):
        """Straight to the mapping page, not the listing.

        Creation has just seeded eight rows by auto-match, and whether it found
        eight or five is a thing only the operator can judge — so they are shown
        it now, while the collection is still in mind, rather than left to
        re-open the publication and find out.
        """
        return reverse("forti_publication_mapping", args=[self.object.pk])


class FortiPublicationEditView(ForecastCollectionsOnlyMixin, EditView):
    def get_success_url(self):
        """Back to the inspect page, where the consequence of the edit shows."""
        return inspect_url(self.object)


class FortiPublicationViewSet(OrgScopedViewSetMixin, SnippetViewSet):
    model = FortiPublication
    icon = "site"
    # "Forti" under a group already called Publications — the page heading and
    # the model keep the longer name, which they carry without a parent.
    menu_label = "Forti"
    # Into core's Publications group, which is what makes this a plugin's entry
    # rather than a plugin deciding it deserves top-level rank in the sidebar.
    menu_hook = PUBLICATIONS_MENU_HOOK
    list_display = ["slug", "collection", "visibility", "status", "published_version", "built_at"]
    list_filter = ["status", "visibility", "is_enabled"]
    index_view_class = FortiPublicationIndexView
    add_view_class = FortiPublicationCreateView
    edit_view_class = FortiPublicationEditView
    inspect_view_enabled = True
    inspect_view_class = FortiPublicationInspectView
    # A copy would carry the name, which is refused as taken, and the area,
    # which is the one thing a second publication of the same collection may
    # not share. There is nothing here worth copying.
    copy_view_enabled = False
    # An ``ObjectList`` rather than ``panels``, for the one thing only it can
    # carry: the base form class, which holds the one mapping rule the form
    # still enforces.
    edit_handler = ObjectList(
        [
            MultiFieldPanel(
                [
                    FieldPanel("collection"),
                    FieldPanel("slug"),
                    FieldPanel("visibility"),
                    FieldPanel("is_enabled"),
                ],
                heading="Forecast",
                help_text="Which collection to publish, and what to call it in the Forti app.",
            ),
            MultiFieldPanel(
                [
                    FieldPanel("west"),
                    FieldPanel("south"),
                    FieldPanel("east"),
                    FieldPanel("north"),
                ],
                heading="Area",
                help_text=(
                    "The area the forecast covers. Include a margin past your borders. "
                    "Cannot be changed after the first publish."
                ),
            ),
            MultiFieldPanel(
                [
                    FieldPanel("time_until_next_hours"),
                    FieldPanel("generation"),
                ],
                heading="Advanced",
                help_text="Usually left alone.",
            ),
        ],
        base_form_class=forms.FortiPublicationForm,
    )


register_snippet(FortiPublicationViewSet)


@hooks.register("register_admin_urls")
def register_forti_admin_urls():
    return [
        path(
            "forti/publication/<int:publication_pk>/mapping/",
            mapping_page,
            name="forti_publication_mapping",
        ),
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
        _("Forecast serving status"),
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
            {"url": None, "label": _("Forecast serving status")},
        ],
        "header_title": _("Forecast serving status"),
        "header_icon": "site",
        "report": verification.report(),
    }
    return render(request, "georiva_publisher_forti/verification_panel.html", context)


def inspect_url(publication) -> str:
    """Where every save of a publication returns to — see the module docstring."""
    return reverse(f"{SNIPPET}:inspect", args=[publication.pk])


def _scoped_publication(request, publication_pk):
    """This organisation's publication or a 404, with the two joins every
    hand-written page reads on the way to a breadcrumb."""
    return get_org_object_or_404(
        request,
        FortiPublication.objects.select_related("collection", "collection__catalog"),
        pk=publication_pk,
    )


def mapping_page(request, publication_pk):
    """The eight slots, on the page that knows which collection they are of.

    Reached from the listing, from the inspect page, and as the page a new
    publication lands on. Saving returns to the inspect page: every path in
    here ends with the same question — is it ready now? — and that page's
    readiness is the answer. Everything rendered is decided in :mod:`~.forms`.
    """
    publication = _scoped_publication(request, publication_pk)
    if not can_change(request.user):
        return permission_denied(request)

    if request.method == "POST":
        form = forms.VariableMappingForm(publication, request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, _("The variables of %s are saved.") % publication.slug)
            return redirect(inspect_url(publication))
    else:
        form = forms.VariableMappingForm(publication)

    context = {
        "breadcrumbs_items": [
            {"url": reverse("wagtailadmin_home"), "label": _("Home")},
            {"url": reverse(f"{SNIPPET}:list"), "label": _("Forti publications")},
            {"url": inspect_url(publication), "label": publication.slug},
            {"url": None, "label": _("Variables")},
        ],
        "header_title": _("Variables — %s") % publication.slug,
        "header_icon": "list-ul",
        "publication": publication,
        "form": form,
        "rows": form.mapping_rows(),
        "acknowledgement": form.acknowledgement,
        "incomplete_label": forms.INCOMPLETE_LABEL,
        "checks_made": forms.CHECKS_MADE,
        "checks_not_made": forms.CHECKS_NOT_MADE,
    }
    return render(request, "georiva_publisher_forti/mapping_page.html", context)


@require_POST
def queue_rebuild(request, publication_pk):
    """Re-queue a publication for the next sweep.

    Deliberately not a dispatch. ``queue_rebuild`` is a single conditional UPDATE
    that refuses a publication a worker is actively holding, so an impatient
    operator cannot start a second writer on one area — and the sweep picks the
    row up on its normal cadence either way.

    POST only and gated like the form, as the virtual-Zarr re-queue is: a GET
    that changed state would be one a link preview could trigger.
    """
    publication = _scoped_publication(request, publication_pk)
    if not can_change(request.user):
        return permission_denied(request)

    if publication.queue_rebuild():
        messages.success(request, _("%s will be republished within a few minutes.") % publication.slug)
    else:
        messages.warning(
            request,
            _("%s is being published right now — try again once it finishes.") % publication.slug,
        )

    return redirect(inspect_url(publication))
