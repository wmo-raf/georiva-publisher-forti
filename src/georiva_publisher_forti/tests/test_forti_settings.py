"""Where a ``GEORIVA_FORTI_*`` value comes from, and what a bad one does.

Core ships none of these settings (D19), so on an image-only server the
environment is the only place an operator can set one. The order is a Django
setting, then the environment, then the plugin's default — the Django setting
first so that ``override_settings`` in every other test, and a dev settings
file, keep meaning what they meant.
"""

import os
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from georiva_publisher_forti import forti_settings
from georiva_publisher_forti.forti_settings import read

TIMEOUT = "GEORIVA_FORTI_TIMEOUT"
PORT = "GEORIVA_FORTI_JSONFRONTEND_PORT"
HOST = "GEORIVA_FORTI_JSONFRONTEND_HOST"
RATE = "GEORIVA_FORTI_FORECAST_THROTTLE_RATE"


def env(**values):
    return mock.patch.dict(os.environ, values)


class PrecedenceTests(SimpleTestCase):
    def test_the_default_when_nothing_is_set(self):
        with env():
            os.environ.pop(TIMEOUT, None)
            self.assertEqual(read(TIMEOUT, 5), 5)

    def test_the_environment_over_the_default(self):
        with env(GEORIVA_FORTI_TIMEOUT="12"):
            self.assertEqual(read(TIMEOUT, 5), 12.0)

    @override_settings(GEORIVA_FORTI_TIMEOUT=3)
    def test_a_django_setting_over_the_environment(self):
        with env(GEORIVA_FORTI_TIMEOUT="12"):
            self.assertEqual(read(TIMEOUT, 5), 3)

    @override_settings(GEORIVA_FORTI_FORECAST_THROTTLE_RATE=None)
    def test_a_django_setting_of_none_is_still_a_setting(self):
        """``None`` turns the throttle off, so it must not read as "unset"."""
        with env(GEORIVA_FORTI_FORECAST_THROTTLE_RATE="5/min"):
            self.assertIsNone(read(RATE, "60/min"))

    def test_an_empty_value_is_unset_not_off(self):
        """Compose passes ``${X:-}`` through as an empty string. An operator who
        set nothing must get the default, not a disabled throttle."""
        with env(GEORIVA_FORTI_FORECAST_THROTTLE_RATE="", GEORIVA_FORTI_TIMEOUT="  "):
            self.assertEqual(read(RATE, "60/min"), "60/min")
            self.assertEqual(read(TIMEOUT, 5), 5)


class CastTests(SimpleTestCase):
    def test_numbers_are_numbers(self):
        with env(GEORIVA_FORTI_JSONFRONTEND_PORT="9090", GEORIVA_FORTI_TIMEOUT="2.5"):
            self.assertEqual(read(PORT, 8080), 9090)
            self.assertEqual(read(TIMEOUT, 5), 2.5)

    def test_strings_are_stripped(self):
        with env(GEORIVA_FORTI_JSONFRONTEND_HOST=" forti-jsonfrontend-2 "):
            self.assertEqual(read(HOST, "forti-jsonfrontend"), "forti-jsonfrontend-2")

    def test_off_disables_a_throttle(self):
        for spelling in ("off", "OFF", " Off "):
            with self.subTest(spelling=spelling), env(GEORIVA_FORTI_FORECAST_THROTTLE_RATE=spelling):
                self.assertIsNone(read(RATE, "60/min"))

    def test_a_rate_is_passed_through_as_written(self):
        with env(GEORIVA_FORTI_FORECAST_THROTTLE_RATE="100/hour"):
            self.assertEqual(read(RATE, "60/min"), "100/hour")

    def test_a_malformed_value_names_the_variable(self):
        cases = {
            TIMEOUT: "five",
            PORT: "80.5",
            RATE: "60 per minute",
            "GEORIVA_FORTI_FORECAST_MAX_AGE": "-1",
        }
        for name, raw in cases.items():
            with self.subTest(name=name), env(**{name: raw}):
                with self.assertRaisesMessage(ImproperlyConfigured, name):
                    read(name, None)

    def test_an_unregistered_name_is_refused(self):
        """Every read goes through the registry, so the compose passthrough test
        can trust that the registry is the whole list."""
        with self.assertRaises(KeyError):
            read("GEORIVA_FORTI_NOT_A_SETTING", 1)


class StartupCheckTests(SimpleTestCase):
    def test_a_malformed_value_fails_the_check(self):
        """At startup, not on the first request that happens to read it."""
        with env(GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED="lots"):
            with self.assertRaisesMessage(ImproperlyConfigured, "GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED"):
                forti_settings.check()

    def test_well_formed_and_empty_values_pass(self):
        with env(
            GEORIVA_FORTI_TIMEOUT="7",
            GEORIVA_FORTI_FORECAST_THROTTLE_RATE="off",
            GEORIVA_FORTI_FORECAST_MAX_AGE="",
        ):
            forti_settings.check()

    def test_the_app_runs_the_check_when_it_is_ready(self):
        from django.apps import apps

        with env(GEORIVA_FORTI_TIMEOUT="soon"), self.assertRaises(ImproperlyConfigured):
            apps.get_app_config("georiva_publisher_forti").ready()

    def test_every_setting_is_registered(self):
        self.assertEqual(
            set(forti_settings.SETTINGS),
            {
                "GEORIVA_FORTI_JSONFRONTEND_HOST",
                "GEORIVA_FORTI_JSONFRONTEND_PORT",
                "GEORIVA_FORTI_TIMEOUT",
                "GEORIVA_FORTI_FORECAST_THROTTLE_RATE",
                "GEORIVA_FORTI_FORECAST_THROTTLE_RATE_AUTHENTICATED",
                "GEORIVA_FORTI_FORECAST_MAX_AGE",
                "GEORIVA_FORTI_VERIFICATION_DEADLINE",
                "GEORIVA_FORTI_MAXIMUM_GRIDPOINT_DISTANCE",
            },
        )
