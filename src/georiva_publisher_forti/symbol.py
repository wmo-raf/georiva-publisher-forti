"""The Yr weather symbol, on met.no's own calibration.

The thresholds and the code table are lifted **verbatim** from `forti-prep`'s
`weather_symbol.py`: they are met.no's calibration, and the numeric codes have to
match what `go-weathersymbol` will resolve on the way back out. Reproducing them
from a description would produce numbers that decode to a symbol — just not the
right one.

Two things from that file are deliberately *not* reused:

- **The day/night bit**, which `forti-prep` takes from a hardcoded UTC+02:00 and
  an ``hour < 6 or hour >= 18`` rule (`weather_symbol.py:81`). That is Norway's
  operational shortcut for one time zone. Here it is solar elevation at the
  window's midpoint, which is right everywhere and costs a few trig operations.
- **``_get_fog_code``**, which smooths the values and then thresholds the
  *unsmoothed* ones (`weather_symbol.py:158`). Not used in v1 anyway — there is
  no fog input — but the bug is worth not inheriting.

Fog and thunder are absent from our inputs, so both arrays are zero. The code
table keeps its ``(clouds, droplets, thunder)`` key regardless: it is met.no's
table unaltered, and adding a thunder input later is then a data change rather
than a rewrite.

M0 resolved the rounding question the plan carried: with ``scale_factor`` written
explicitly as ``1.0``, every code survives ``int16 → float32 × factor →
weathersymbol.FromValue`` exactly. 15 distinct valid Yr identifiers, day and night
variants, zero unresolved.
"""

from datetime import timedelta

import numpy as np

# Yr symbol codes.
UNKNOWN = -1
CLEARSKY = 1
FAIR = 2
PARTLYCLOUDY = 3
CLOUDY = 4
RAINSHOWERS = 5
RAINSHOWERSANDTHUNDER = 6
RAIN = 9
HEAVYRAIN = 10
HEAVYRAINANDTHUNDER = 11
FOG = 15
RAINANDTHUNDER = 22
LIGHTRAINSHOWERSANDTHUNDER = 24
HEAVYRAINSHOWERSANDTHUNDER = 25
LIGHTRAINANDTHUNDER = 30
LIGHTRAINSHOWERS = 40
HEAVYRAINSHOWERS = 41
LIGHTRAIN = 46

#: High bit set on the code means the night variant of the symbol.
NIGHT_BIT = 1 << 7

#: (cloud code, droplet code, thunder) → symbol. met.no's table, unaltered.
SYMBOLS = {
    (0, 0, 0): CLEARSKY,
    (1, 0, 0): FAIR,
    (2, 0, 0): PARTLYCLOUDY,
    (2, 1, 0): LIGHTRAINSHOWERS,
    (2, 1, 1): LIGHTRAINSHOWERSANDTHUNDER,
    (2, 2, 0): RAINSHOWERS,
    (2, 2, 1): RAINSHOWERSANDTHUNDER,
    (2, 3, 0): HEAVYRAINSHOWERS,
    (2, 3, 1): HEAVYRAINSHOWERSANDTHUNDER,
    (3, 0, 0): CLOUDY,
    (3, 1, 0): LIGHTRAIN,
    (3, 1, 1): LIGHTRAINANDTHUNDER,
    (3, 2, 0): RAIN,
    (3, 2, 1): RAINANDTHUNDER,
    (3, 3, 0): HEAVYRAIN,
    (3, 3, 1): HEAVYRAINANDTHUNDER,
}

#: Millimetres over the window, per window length. Calibrated per span, because
#: "how much rain counts as heavy" is a rate: 1 mm in an hour and 1 mm in twelve
#: are different weather. Using the 6-hour table for a 12-hour window would
#: overstate every symbol.
DROPLET_THRESHOLDS = {
    1: (0.1, 0.25, 0.95),
    6: (0.5, 0.95, 4.95),
    12: (1.0, 1.9, 9.9),
}

#: Percent cloud cover. One table for every span — met.no varies the *smoothing*
#: with the span, not the thresholds.
CLOUD_THRESHOLDS = (13, 38, 86)


def droplet_code(precipitation_mm: np.ndarray, hours: int) -> np.ndarray:
    """0–3, from the millimetres that fell over a window of ``hours``."""
    try:
        light, moderate, heavy = DROPLET_THRESHOLDS[hours]
    except KeyError:
        raise ValueError(
            f"No met.no droplet calibration for a {hours}-hour window "
            f"(have {sorted(DROPLET_THRESHOLDS)}). Publishing one would mean inventing "
            f"thresholds, and the symbol would be wrong in a way nothing downstream "
            f"can detect."
        ) from None

    code = np.zeros(precipitation_mm.shape, dtype=np.int16)
    code[precipitation_mm > light] = 1
    code[precipitation_mm > moderate] = 2
    code[precipitation_mm > heavy] = 3
    return code


def cloud_code(cover_percent: np.ndarray) -> np.ndarray:
    """0–3, from cloud cover in percent."""
    light, moderate, heavy = CLOUD_THRESHOLDS
    code = np.zeros(cover_percent.shape, dtype=np.int16)
    code[cover_percent > light] = 1
    code[cover_percent > moderate] = 2
    code[cover_percent > heavy] = 3
    return code


def solar_elevation(times, latitude: np.ndarray, longitude: np.ndarray) -> np.ndarray:
    """Sun elevation in degrees, ``(time, point)``.

    NOAA's low-precision equations — good to a fraction of a degree, which is far
    more than deciding day from night needs. What it buys over a fixed-offset
    rule is being correct outside the one time zone the rule was written for, and
    correct near the poles, where "before 18:00" and "after sunset" part company
    entirely.
    """
    elevation = np.empty((len(times), latitude.size), dtype=np.float64)
    latitude_rad = np.radians(latitude)

    for index, moment in enumerate(times):
        day_of_year = moment.timetuple().tm_yday
        fractional_year = 2 * np.pi / 365.0 * (day_of_year - 1 + (moment.hour - 12) / 24.0)

        declination = (
            0.006918
            - 0.399912 * np.cos(fractional_year)
            + 0.070257 * np.sin(fractional_year)
            - 0.006758 * np.cos(2 * fractional_year)
            + 0.000907 * np.sin(2 * fractional_year)
        )
        equation_of_time = 229.18 * (
            0.000075
            + 0.001868 * np.cos(fractional_year)
            - 0.032077 * np.sin(fractional_year)
            - 0.014615 * np.cos(2 * fractional_year)
            - 0.040849 * np.sin(2 * fractional_year)
        )

        true_solar_time = (moment.hour * 60 + moment.minute + equation_of_time + 4 * longitude) % 1440
        hour_angle = np.radians(true_solar_time / 4.0 - 180.0)

        elevation[index] = np.degrees(
            np.arcsin(
                np.sin(latitude_rad) * np.sin(declination)
                + np.cos(latitude_rad) * np.cos(declination) * np.cos(hour_angle)
            )
        )

    return elevation


def weather_symbol(
    precipitation_mm: np.ndarray,
    cloud_percent: np.ndarray,
    end_times,
    latitude: np.ndarray,
    longitude: np.ndarray,
    hours: int,
) -> np.ndarray:
    """The symbol for each window, ``(window, point)``.

    ``precipitation_mm`` is what fell during each window and ``cloud_percent`` is
    the mean cover across it — both already reduced to one value per window, so
    both arrays are indexed by window, not by timestep. ``end_times`` are the
    windows' end times, which is what ``meta.json`` carries.
    """
    droplets = droplet_code(precipitation_mm, hours)
    clouds = cloud_code(cloud_percent)

    # met.no's rule: there is no such thing as light cloud with rain falling out
    # of it. Applied before the lookup, exactly as `_calculate_symbol` does.
    clouds[(clouds <= 1) & (droplets > 0)] = 2

    thunder = np.zeros(droplets.shape, dtype=np.int16)

    codes = np.zeros(droplets.shape, dtype=np.int16)
    for (cloud, droplet, thunder_flag), symbol in SYMBOLS.items():
        codes[(clouds == cloud) & (droplets == droplet) & (thunder == thunder_flag)] = symbol

    # Day or night at the middle of the window the symbol describes — the window
    # runs backwards from its end time, so the midpoint is end - hours/2.
    midpoints = [end - timedelta(hours=hours / 2) for end in end_times]
    codes[solar_elevation(midpoints, latitude, longitude) < 0] |= NIGHT_BIT

    return codes
