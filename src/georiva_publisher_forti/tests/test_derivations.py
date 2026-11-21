"""Series computed after the transpose, and the two ways they go silently wrong.

Both failures here produce plausible numbers rather than errors, which is why
they are tested rather than reviewed: `forti-prep`'s `convert.py:128` has used
``np.diff(acc, n=6)`` — the sixth-order finite difference — instead of
``acc[6:] - acc[:-6]`` for as long as it has existed, and the length happens to
match, so its own assert passes.
"""

from datetime import UTC, datetime, timedelta

import numpy as np
from django.test import SimpleTestCase

from georiva_publisher_forti.derivations import (
    accumulation_window,
    relative_humidity,
    wind_from_direction,
    wind_speed,
    window_max,
    window_mean,
    window_min,
)
from georiva_publisher_forti.windows import find_windows

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)


def hourly(count):
    return [BASE + timedelta(hours=hour) for hour in range(count)]


class PrecipitationWindowTests(SimpleTestCase):
    def test_a_window_is_the_difference_between_its_endpoints(self):
        times = hourly(13)
        accumulated = np.arange(13, dtype=float).reshape(13, 1)

        fell = accumulation_window(accumulated, find_windows(times, 6))

        # 1 mm an hour for six hours, every window.
        self.assertEqual(fell.ravel().tolist(), [6.0] * 7)

    def test_it_is_not_the_nth_order_finite_difference(self):
        """``np.diff(acc, n=6)`` comes out the right length and the wrong number
        — which is exactly why forti-prep's assert never caught it."""
        times = hourly(13)
        accumulated = np.arange(13, dtype=float).reshape(13, 1)

        ours = accumulation_window(accumulated, find_windows(times, 6))
        theirs = np.diff(accumulated, n=6, axis=0)

        self.assertEqual(ours.shape, theirs.shape)
        self.assertNotEqual(ours.ravel().tolist(), theirs.ravel().tolist())

    def test_a_field_that_is_not_an_accumulation_is_refused(self):
        times = hourly(13)
        per_step = np.tile(np.array([0.0, 5.0, 0.0]), 5)[:13].reshape(13, 1)

        with self.assertRaises(ValueError) as ctx:
            accumulation_window(per_step, find_windows(times, 6))

        self.assertIn("not accumulating", str(ctx.exception))

    def test_float_noise_in_an_accumulation_is_tolerated(self):
        times = hourly(13)
        accumulated = np.arange(13, dtype=float).reshape(13, 1)
        accumulated[5] += 0.001  # a rounding wobble, not a different quantity

        accumulation_window(accumulated, find_windows(times, 6))

    def test_a_grib_packing_dip_publishes_no_negative_rain(self):
        """ECMWF's 12-bit packing rounds each step on its own, so a point can
        fall by a whole quantum (0.122 mm) between steps. That publishes, and a
        window ending in the dip reads as the rain that fell, never below zero."""
        times = hourly(13)
        accumulated = np.full((13, 1), 10.0)
        accumulated[:7] = 10.122  # rounded up early, down from step 7 on

        result = accumulation_window(accumulated, find_windows(times, 6))

        self.assertTrue((result >= 0).all())

    def test_a_dry_window_across_a_dip_is_exactly_zero(self):
        times = hourly(13)
        accumulated = np.full((13, 1), 4.0)
        accumulated[3] = 4.122  # rounded up once, then back
        windows = find_windows(times, 6)
        from_the_dip = next(i for i, window in enumerate(windows) if window.start == 3)

        result = accumulation_window(accumulated, windows)

        # Raw, this window is 4.0 - 4.122: negative rain.
        self.assertEqual(result[from_the_dip].item(), 0.0)


class ExtremeTests(SimpleTestCase):
    def test_the_maximum_includes_both_endpoints(self):
        times = hourly(7)
        values = np.array([[1.0], [9.0], [2.0], [3.0], [4.0], [5.0], [6.0]])

        self.assertEqual(window_max(values, find_windows(times, 6)).ravel().tolist(), [9.0])

    def test_the_minimum_includes_both_endpoints(self):
        times = hourly(7)
        values = np.array([[1.0], [9.0], [2.0], [3.0], [4.0], [5.0], [6.0]])

        self.assertEqual(window_min(values, find_windows(times, 6)).ravel().tolist(), [1.0])

    def test_the_mean_is_taken_over_the_steps_the_window_spans(self):
        """met.no smooths cloud cover over ``time_resolution`` *array elements*,
        which is the window length in hours only for hourly data. Ours is not."""
        times = [BASE + timedelta(hours=hour) for hour in (0, 3, 6)]
        values = np.array([[0.0], [60.0], [0.0]])

        self.assertEqual(window_mean(values, find_windows(times, 6)).ravel().tolist(), [20.0])


class RelativeHumidityTests(SimpleTestCase):
    def test_saturated_air_is_a_hundred_percent(self):
        humidity = relative_humidity(np.array([[20.0]]), np.array([[20.0]]))

        self.assertAlmostEqual(float(humidity[0, 0]), 100.0, places=6)

    def test_a_lower_dew_point_means_drier_air(self):
        humidity = relative_humidity(np.array([[25.0]]), np.array([[20.0]]))

        self.assertTrue(0 < float(humidity[0, 0]) < 100)

    def test_a_dew_point_above_the_temperature_is_clipped_not_refused(self):
        """Reported in saturated air, and not worth failing a publish over."""
        humidity = relative_humidity(np.array([[20.0]]), np.array([[21.0]]))

        self.assertEqual(float(humidity[0, 0]), 100.0)


class WindFromComponentsTests(SimpleTestCase):
    """Speed and direction from 10 m u and v, for a collection that holds only
    the components — as a store harvest does, mapping one array to one Variable.

    Direction is the meteorological one, the bearing the wind blows *from*, the
    convention core's vector-direction transform writes at ingestion: u is
    eastward and v northward, so a wind blowing towards the south comes from 0°.
    """

    def components(self, *pairs):
        u = np.array([[u for u, _ in pairs]], dtype=np.float32)
        v = np.array([[v for _, v in pairs]], dtype=np.float32)
        return u, v

    def test_speed_is_the_vector_magnitude(self):
        u, v = self.components((3.0, 4.0), (-3.0, -4.0), (0.0, -7.5))

        np.testing.assert_allclose(wind_speed(u, v), [[5.0, 5.0, 7.5]], atol=1e-6)

    def test_a_wind_from_each_compass_point(self):
        """From the north blows southwards: v negative. From the east blows westwards: u negative."""
        u, v = self.components((0.0, -5.0), (-5.0, 0.0), (0.0, 5.0), (5.0, 0.0))

        np.testing.assert_allclose(wind_from_direction(u, v), [[0.0, 90.0, 180.0, 270.0]], atol=1e-4)

    def test_a_wind_from_each_quadrant(self):
        u, v = self.components((-3.0, -3.0), (-3.0, 3.0), (3.0, 3.0), (3.0, -3.0))

        np.testing.assert_allclose(wind_from_direction(u, v), [[45.0, 135.0, 225.0, 315.0]], atol=1e-4)

    def test_direction_stays_below_a_full_circle(self):
        """A wind a hair west of north rounds into [0, 360), never to 360 itself."""
        u, v = self.components((1e-12, -5.0), (1e-3, -5.0))

        direction = wind_from_direction(u, v)

        self.assertTrue(((direction >= 0.0) & (direction < 360.0)).all())

    def test_calm_has_no_speed_and_the_direction_core_gives_it(self):
        """No wind has no direction worth reading; it is given the bearing core's
        transform computes for zero components, so a map and a point forecast of
        one calm cell agree."""
        u, v = self.components((0.0, 0.0))

        self.assertEqual(wind_speed(u, v).tolist(), [[0.0]])
        self.assertEqual(wind_from_direction(u, v).tolist(), [[180.0]])
