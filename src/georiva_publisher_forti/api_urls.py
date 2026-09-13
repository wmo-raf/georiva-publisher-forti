"""The routes this plugin contributes under ``/api/``.

Core discovers this module by name and includes its ``urlpatterns`` — that
convention is the whole of the registration, with no registry to append to and
no settings entry to add (core's ADR 0028). Two consequences worth knowing here:

- **Core's patterns are matched first.** Nothing below could shadow
  ``/api/stac/`` even if it tried; a plugin pattern that collides with one of
  core's is simply never reached.
- **A plugin whose ``api_urls`` will not import is logged and skipped, not
  fatal.** So a route from this file that 404s may be an import error in
  :mod:`serving` rather than a bad pattern — read the ``georiva.core.plugins``
  log at ERROR before debugging a regex.

``app_name`` is declared so these two names are reversed as
``georiva_publisher_forti:forecast``; the *paths* carry no package name, because
a public API route is a contract with somebody else's client rather than a place
to write down our packaging.
"""

from django.urls import path

from .serving import ForecastModelListView, ForecastView

app_name = "georiva_publisher_forti"

urlpatterns = [
    # The listing first, though the resolver does not need it to be: `forecast/`
    # and `forecast/<slug:model>/` cannot both match one path. It reads in the
    # order a consumer meets them.
    path("forecast/", ForecastModelListView.as_view(), name="forecast_models"),
    path("forecast/<slug:model>/", ForecastView.as_view(), name="forecast"),
]
