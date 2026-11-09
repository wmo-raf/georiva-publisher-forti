"""The plugin's settings, and the one rule for where each value comes from.

Core ships no ``GEORIVA_FORTI_*`` setting (D19), so on an image-only server a
Django setting is out of an operator's reach and the environment is the only
place one can be set. ``deploy/compose.yml`` passes every name registered here
into the core services that read them; ``test_compose.py`` holds the two lists
equal.

Order, first match wins:

1. a **Django setting** of that name, whatever its value — ``None`` included,
   because ``None`` is how a throttle is turned off there. First so that
   ``override_settings`` and a dev settings file keep meaning what they meant;
2. the **environment**, cast. An empty or blank value is *unset*: compose passes
   ``${X:-}`` through as ``""``, and an operator who set nothing must get the
   default, not a disabled throttle. ``off`` is how the environment spells
   ``None`` for a rate;
3. the **default**, which stays beside the code that uses it.

A malformed value raises ``ImproperlyConfigured`` naming the variable, and
:func:`check` runs every cast from ``AppConfig.ready()`` so that a typo stops the
process at startup instead of turning the first forecast request into a 500.
"""

import os
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

OFF = "off"

# DRF's ``SimpleRateThrottle.parse_rate``: an integer, a slash, and a period
# whose first letter is all it reads.
_RATE = re.compile(r"^\d+/[smhd][a-z]*$")


def _text(raw: str) -> str:
    return raw


def _positive_float(raw: str) -> float:
    value = float(raw)
    if not value > 0:
        raise ValueError("must be greater than 0")
    return value


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise ValueError("must not be negative")
    return value


def _port(raw: str) -> int:
    value = int(raw)
    if not 0 < value < 65536:
        raise ValueError("must be a port number")
    return value


def _rate(raw: str) -> str | None:
    if raw.lower() == OFF:
        return None
    if not _RATE.match(raw):
        raise ValueError(f'must be a rate like "60/min", or "{OFF}"')
    return raw


#: Every setting the plugin reads, and how its environment value is parsed.
SETTINGS = {
    "GEORIVA_FORTI_JSONFRONTEND_HOST": _text,
    "GEORIVA_FORTI_JSONFRONTEND_PORT": _port,
    "GEORIVA_FORTI_TIMEOUT": _positive_float,
    "GEORIVA_FORTI_FORECAST_THROTTLE_RATE": _rate,
    "GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED": _rate,
    "GEORIVA_FORTI_FORECAST_MAX_AGE": _non_negative_int,
    "GEORIVA_FORTI_VERIFICATION_DEADLINE": _positive_float,
    "GEORIVA_FORTI_MAXIMUM_GRIDPOINT_DISTANCE": _non_negative_int,
}

_MISSING = object()


def _from_environment(name: str):
    """The cast environment value, or ``_MISSING`` when it is unset or blank."""
    cast = SETTINGS[name]
    raw = os.environ.get(name, "").strip()
    if not raw:
        return _MISSING
    try:
        return cast(raw)
    except ValueError as error:
        raise ImproperlyConfigured(f"{name}={raw!r}: {error}") from error


def read(name: str, default):
    """The value of ``name``: Django setting, then environment, then ``default``.

    ``name`` must be registered in :data:`SETTINGS` — a ``KeyError`` otherwise —
    so that the registry, and with it the compose passthrough, is the whole list.
    """
    SETTINGS[name]
    if hasattr(settings, name):
        return getattr(settings, name)
    value = _from_environment(name)
    return default if value is _MISSING else value


def check() -> None:
    """Parse every environment value now. Raises on the first malformed one."""
    for name in SETTINGS:
        _from_environment(name)
