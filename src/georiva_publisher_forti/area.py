"""The Area panel: what it offers an operator, decided in Python.

The publication's extent is four floats and stays four floats — the model, its
``clean()`` and the grid pinned from them are untouched. This module is the help
around them: a list of countries to start from, the footprints of the collection's
own recent items, and the panel that renders both beside the inputs.

Two sources, and the difference between them is a decision, not a detail:

* A **country** is a place to start, so it is padded by :data:`COUNTRY_MARGIN`
  degrees — a point forecast for a border town is asked for from both sides of
  the border, and an area that stops at the line answers "outside coverage" to
  half of them (the same reason ``seed_extent_from_catalog`` pads).
* An **item** is the footprint of data that exists, so it is taken exactly. Padding
  it would claim points the collection has no values for.

Nothing here is stored. Picking fills the inputs and the inputs are what is
saved, so a stale list can mislead an operator for a moment and cannot corrupt
anything.
"""

from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from django_countries import countries
from django_countries.fields import Country
from django_countries_geoextent import get_geo_extent
from wagtail.admin.panels import FieldPanel, MultiFieldPanel

from georiva.core.models import Item

#: Degrees added to every side of a picked country. Matches
#: :meth:`~.models.FortiPublication.seed_extent_from_catalog`.
COUNTRY_MARGIN = 0.5

AREA_FIELDS = ("west", "south", "east", "north")

#: How many distinct footprints the item picker shows.
ITEM_CHOICES = 10

#: How many recent items are read to find them. Items of one collection almost
#: always share a grid, so a window of a few hundred is "every footprint this
#: collection has recently had" without reading the hypertable.
ITEM_WINDOW = 300


def country_extents() -> list[dict]:
    """Every country with a known extent, as ``{code, name, bbox}`` by name.

    ``bbox`` is the country's own, unpadded: the margin is applied where the
    pick happens, so what the page shows can say what was added.
    """
    found = []
    for entry in countries:
        country = Country(code=entry.code)
        bbox = get_geo_extent(country)
        if not bbox:
            continue
        found.append({"code": country.code, "name": str(country.name), "bbox": [float(v) for v in bbox]})
    found.sort(key=lambda entry: entry["name"])
    return found


def recent_item_extents(collection, limit: int = ITEM_CHOICES) -> list[dict]:
    """The distinct footprints of ``collection``'s newest items, newest first.

    Items with the same bounds collapse into the newest of them. Compared after
    rounding, so a footprint that differs in the ninth decimal is not offered as
    though it were another grid.
    """
    items = (
        Item.objects.filter(collection=collection, bounds__isnull=False)
        .order_by("-time")
        .values_list("time", "bounds")[:ITEM_WINDOW]
    )
    seen = set()
    distinct = []
    for time, bounds in items:
        if len(bounds) != 4:
            continue
        key = tuple(round(v, 6) for v in bounds)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(
            {
                "label": date_format(time, "DATETIME_FORMAT"),
                "bbox": list(key),
            }
        )
        if len(distinct) == limit:
            break
    return distinct


class AreaPanel(MultiFieldPanel):
    """The four edges, with a country chooser, an item chooser and a map.

    A ``MultiFieldPanel`` whose only difference is what surrounds its children,
    so the form fields, their errors and their layout are Wagtail's own.
    """

    def __init__(self, children=(), **kwargs):
        # ``clone()`` hands the children back, so they are only defaulted.
        super().__init__(children or [FieldPanel(name) for name in AREA_FIELDS], **kwargs)

    class BoundPanel(MultiFieldPanel.BoundPanel):
        template_name = "georiva_publisher_forti/panels/area.html"

        def get_context_data(self, parent_context=None):
            context = super().get_context_data(parent_context)
            instance = self.instance
            context.update(
                countries=country_extents(),
                margin=COUNTRY_MARGIN,
                locked=bool(instance and instance.pk and instance.grid_id),
                # Where the page starts: a stored area opens in Custom, and so
                # does a form the server sent back with an edge refused — the
                # inputs and their errors have to be visible.
                saved=bool(instance and instance.pk),
                has_errors=any(self.form.errors.get(name) for name in AREA_FIELDS),
                items_url=reverse_items_url(),
            )
            return context


def reverse_items_url() -> str:
    from django.urls import reverse

    return reverse("forti_area_items")


def locked_extent_error(instance, cleaned) -> dict:
    """``{field: message}`` for every edge changed on a publication that has a grid.

    Read from the database rather than the instance: by the time a form's
    ``clean()`` runs, a model form has already written the posted values onto the
    instance, so the instance can no longer say what was stored.
    """
    if not instance.pk or not instance.grid_id:
        return {}
    stored = type(instance).objects.filter(pk=instance.pk).values(*AREA_FIELDS).first()
    if stored is None:
        return {}
    return {
        name: _("The area is fixed after the first publish.")
        for name in AREA_FIELDS
        if name in cleaned and cleaned[name] != stored[name]
    }
