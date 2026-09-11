"""The config that turns Forti's bytes back into locationforecast 2.0.

Two of its numbers are copied from met.no's *production* config rather than from
the jsonfrontend README, and both were wrong in the plan before M0:

``offset`` is the window's length in hours — 0 / 1 / 6 / 12. The README's example
puts ``next_6_hours`` at ``offset: 0``, which collides with ``instant``, because
``ret[duration]`` (`encode.go:180`) is keyed by duration alone.

``data_expiry_offset`` is 1800. The plan had pencilled in 180.
"""

from django.test import SimpleTestCase, TestCase

from georiva_publisher_forti.jsonformat import DATA_EXPIRY_OFFSET, build

from .factories import make_collection, make_publication


class Published:
    """Just enough of a publication for ``build``."""

    def __init__(self, *names):
        self.published_parameters = list(names)
        self.is_enabled = True


class GroupTests(SimpleTestCase):
    def test_a_parameter_lands_in_its_own_duration_bucket(self):
        config = build([Published("air_temperature_2m", "precipitation_amount_acc6h")])

        self.assertIn("instant", config["parameters"])
        self.assertIn("next_6_hours", config["parameters"])

    def test_offset_is_the_window_length_in_hours(self):
        config = build([Published("air_temperature_2m", "precipitation_amount_acc6h", "weather_symbol_12h")])

        self.assertEqual(config["parameters"]["instant"]["offset"], 0)
        self.assertEqual(config["parameters"]["next_6_hours"]["offset"], 6)
        self.assertEqual(config["parameters"]["next_12_hours"]["offset"], 12)

    def test_instant_and_a_period_group_never_share_an_offset(self):
        """The README's example does, and ret[duration] is keyed by duration."""
        config = build([Published("air_temperature_2m", "precipitation_amount_acc6h")])

        offsets = [group["offset"] for group in config["parameters"].values()]

        self.assertEqual(len(offsets), len(set(offsets)))

    def test_a_parameter_maps_to_its_locationforecast_name(self):
        config = build([Published("air_temperature_2m")])

        self.assertEqual(config["parameters"]["instant"]["parameters"]["air_temperature_2m"], "air_temperature")

    def test_the_symbol_goes_under_summary_not_parameters(self):
        config = build([Published("weather_symbol_6h")])
        group = config["parameters"]["next_6_hours"]

        self.assertEqual(group["summary"], {"symbol_code": "weather_symbol_6h"})
        self.assertNotIn("weather_symbol_6h", group["parameters"])

    def test_a_group_with_nothing_in_it_is_left_out(self):
        config = build([Published("air_temperature_2m")])

        self.assertEqual(list(config["parameters"]), ["instant"])

    def test_a_name_this_plugin_no_longer_knows_is_dropped(self):
        """Rather than described wrongly: jsonfrontend would go looking for a
        parameter we cannot say anything about."""
        config = build([Published("air_temperature_2m", "some_retired_parameter")])

        self.assertNotIn("some_retired_parameter", config["parameters"]["instant"]["parameters"])


class SettingsTests(SimpleTestCase):
    def test_data_expiry_offset_is_met_nos_production_value(self):
        self.assertEqual(build([Published("air_temperature_2m")])["data_expiry_offset"], 1800)
        self.assertEqual(DATA_EXPIRY_OFFSET, 1800)

    def test_timesteps_before_now_are_cut(self):
        """Otherwise a six-hour-old run leads with the past."""
        self.assertTrue(build([Published("air_temperature_2m")])["cut_forecast"])

    def test_altitude_correction_is_skipped_while_no_dem_is_wired_up(self):
        self.assertTrue(build([Published("air_temperature_2m")])["skip_altitude"])


class UnionTests(TestCase):
    def test_the_config_is_the_union_over_every_area_on_the_instance(self):
        """One jsonfrontend fronts one prefix holding every organisation's areas
        (D20), so there is one document and it is their union. Safe because the
        name → bucket mapping is a function of the parameter name and not of who
        published it."""
        coarse = make_publication(make_collection(slug="global"), slug="global")
        fine = make_publication(make_collection(org_slug="other-org"), slug="national")

        self.assertNotEqual(coarse.organisation, fine.organisation)
        coarse.published_parameters = ["air_temperature_2m"]
        fine.published_parameters = ["precipitation_amount_acc6h"]

        config = build([coarse, fine])

        self.assertIn("instant", config["parameters"])
        self.assertIn("next_6_hours", config["parameters"])
