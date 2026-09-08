"""Period windows are found on valid times, never on step counts.

This is the mistake M0 actually made, and it is the reason this module exists at
all. The spike assumed a 6-hourly feed, computed 3-hour windows on a 3-hourly run,
labelled them ``next_6_hours``, and jsonfrontend duly filed them as "the 6 hours
starting at …" — wrong length, wrong start, and no error anywhere, because every
layer did exactly what it was told.

ECMWF's spacing is 3-hourly to 144 h and 6-hourly beyond, so a 6-hour window is
**two steps early in the run and one step later on**. A fixed step count is
correct for exactly one half of every run, which is the worst possible kind of
wrong: it works in every short test.
"""

from datetime import UTC, datetime, timedelta

from django.test import SimpleTestCase

from georiva_publisher_forti import parameters as params
from georiva_publisher_forti.windows import find_windows, publishable

BASE = datetime(2026, 9, 2, 12, tzinfo=UTC)


def ecmwf_times():
    """The real shape: 3-hourly to 144 h, 6-hourly to 240 h."""
    hours = list(range(0, 145, 3)) + list(range(150, 241, 6))
    return [BASE + timedelta(hours=hour) for hour in hours]


class WindowSpanTests(SimpleTestCase):
    def test_one_run_has_windows_of_two_different_step_counts(self):
        windows = find_windows(ecmwf_times(), 6)

        self.assertEqual(sorted({window.span for window in windows}), [1, 2])

    def test_a_window_always_covers_its_stated_number_of_hours(self):
        times = ecmwf_times()

        for window in find_windows(times, 6):
            with self.subTest(end=window.end_time):
                self.assertEqual(times[window.end] - times[window.start], timedelta(hours=6))

    def test_times_carry_the_window_end_not_its_start(self):
        times = ecmwf_times()

        first = find_windows(times, 6)[0]

        self.assertEqual(first.end_time, times[0] + timedelta(hours=6))

    def test_windows_come_out_ordered_by_end_time(self):
        ends = [window.end_time for window in find_windows(ecmwf_times(), 6)]

        self.assertEqual(ends, sorted(ends))


class WindowExistenceTests(SimpleTestCase):
    def test_a_three_hourly_run_has_no_one_hour_window(self):
        self.assertEqual(find_windows(ecmwf_times(), 1), [])

    def test_a_run_shorter_than_the_window_has_none(self):
        times = [BASE, BASE + timedelta(hours=3)]

        self.assertEqual(find_windows(times, 6), [])

    def test_the_six_hourly_tail_still_yields_twelve_hour_windows(self):
        tail = [BASE + timedelta(hours=hour) for hour in range(150, 241, 6)]

        self.assertTrue(find_windows(tail, 12))

    def test_a_zero_length_window_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            find_windows(ecmwf_times(), 0)


class PublishableParameterTests(SimpleTestCase):
    def test_a_three_hourly_run_publishes_six_hour_series_and_no_one_hour_ones(self):
        names = {parameter.name for parameter in publishable(ecmwf_times(), params.ALL_PARAMETERS)}

        self.assertIn("precipitation_amount_acc6h", names)
        self.assertIn("weather_symbol_6h", names)
        self.assertNotIn("precipitation_amount_acc1h", names)
        self.assertNotIn("weather_symbol", names)

    def test_instant_parameters_survive_any_time_index(self):
        names = {parameter.name for parameter in publishable([BASE], params.ALL_PARAMETERS)}

        self.assertIn("air_temperature_2m", names)
        self.assertIn("relative_humidity_2m", names)

    def test_a_run_with_no_period_window_publishes_no_period_parameter(self):
        chosen = publishable([BASE], params.ALL_PARAMETERS)

        self.assertFalse([parameter for parameter in chosen if parameter.is_period])
