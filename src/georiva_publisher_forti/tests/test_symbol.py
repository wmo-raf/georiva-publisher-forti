"""The Yr symbol, on met.no's calibration rather than ours.

The numeric codes have to match what ``go-weathersymbol`` resolves on the way back
out, so what is pinned here is the *table*, not an interpretation of it. M0 put 15
distinct identifiers through the real Go resolver — day and night variants, zero
unresolved — with ``scale_factor`` written explicitly as 1.0.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
from django.test import SimpleTestCase

from georiva_publisher_forti.symbol import (
    CLEARSKY,
    CLOUDY,
    HEAVYRAIN,
    LIGHTRAINSHOWERS,
    NIGHT_BIT,
    PARTLYCLOUDY,
    cloud_code,
    droplet_code,
    solar_elevation,
    weather_symbol,
)

NAIROBI = (-1.2864, 36.8172)
NOON = datetime(2026, 9, 2, 12, tzinfo=UTC)
MIDNIGHT = datetime(2026, 9, 2, 0, tzinfo=UTC)


class CalibrationTests(SimpleTestCase):
    def test_the_droplet_thresholds_are_per_window_length(self):
        """1 mm in an hour and 1 mm in twelve hours are different weather."""
        one_mm = np.array([1.0])

        self.assertEqual(int(droplet_code(one_mm, 1)[0]), 3)
        self.assertEqual(int(droplet_code(one_mm, 6)[0]), 2)
        self.assertEqual(int(droplet_code(one_mm, 12)[0]), 0)

    def test_a_window_met_no_never_calibrated_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            droplet_code(np.array([1.0]), 3)

        self.assertIn("calibration", str(ctx.exception))

    def test_cloud_cover_codes_at_met_nos_boundaries(self):
        codes = cloud_code(np.array([0.0, 13.0, 13.1, 38.0, 38.1, 86.0, 86.1, 100.0]))

        self.assertEqual(codes.tolist(), [0, 0, 1, 1, 2, 2, 3, 3])


class SymbolTests(SimpleTestCase):
    def _symbol(self, precipitation, cloud, hours=6, at=NOON):
        latitude = np.array([NAIROBI[0]], dtype=np.float32)
        longitude = np.array([NAIROBI[1]], dtype=np.float32)
        codes = weather_symbol(
            np.array([[precipitation]]),
            np.array([[cloud]]),
            [at],
            latitude,
            longitude,
            hours,
        )
        return int(codes[0, 0])

    def test_dry_and_clear_is_clearsky(self):
        self.assertEqual(self._symbol(0.0, 0.0), CLEARSKY)

    def test_dry_and_overcast_is_cloudy(self):
        self.assertEqual(self._symbol(0.0, 95.0), CLOUDY)

    def test_dry_and_broken_is_partlycloudy(self):
        self.assertEqual(self._symbol(0.0, 50.0), PARTLYCLOUDY)

    def test_heavy_rain_under_overcast_is_heavyrain(self):
        self.assertEqual(self._symbol(10.0, 95.0), HEAVYRAIN)

    def test_there_is_no_light_cloud_with_rain_falling_out_of_it(self):
        """met.no promotes the cloud code rather than emitting clear-sky rain."""
        self.assertEqual(self._symbol(0.6, 0.0), LIGHTRAINSHOWERS)

    def test_night_sets_the_high_bit(self):
        self.assertTrue(self._symbol(0.0, 0.0, at=MIDNIGHT) & NIGHT_BIT)

    def test_day_does_not(self):
        self.assertFalse(self._symbol(0.0, 0.0, at=NOON) & NIGHT_BIT)

    def test_the_night_bit_leaves_the_symbol_underneath_intact(self):
        self.assertEqual(self._symbol(0.0, 0.0, at=MIDNIGHT) & ~NIGHT_BIT, CLEARSKY)

    def test_day_and_night_are_decided_at_the_middle_of_the_window(self):
        """A 6-hour window ending at 03:00 UTC over Nairobi (UTC+3) is the small
        hours, not the dawn its end time alone suggests."""
        end = datetime(2026, 9, 2, 3, tzinfo=UTC)

        self.assertTrue(self._symbol(0.0, 0.0, at=end) & NIGHT_BIT)


class SolarElevationTests(SimpleTestCase):
    def test_the_sun_is_up_at_local_noon_and_down_at_local_midnight(self):
        latitude = np.array([NAIROBI[0]], dtype=np.float32)
        longitude = np.array([NAIROBI[1]], dtype=np.float32)

        # Nairobi is UTC+3, so local noon is 09:00 UTC.
        day = solar_elevation([NOON - timedelta(hours=3)], latitude, longitude)
        night = solar_elevation([MIDNIGHT - timedelta(hours=3)], latitude, longitude)

        self.assertGreater(float(day[0, 0]), 0)
        self.assertLess(float(night[0, 0]), 0)

    def test_it_is_not_the_fixed_two_hour_offset_forti_prep_assumes(self):
        """`forti-prep` hardcodes UTC+02:00 and ``hour < 6 or hour >= 18``. At
        180 degrees east that rule is twelve hours out of phase."""
        latitude = np.array([0.0], dtype=np.float32)
        east = np.array([179.0], dtype=np.float32)

        # 00:00 UTC is local noon on the far side of the dateline.
        elevation = solar_elevation([MIDNIGHT], latitude, east)

        self.assertGreater(float(elevation[0, 0]), 0)
