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
