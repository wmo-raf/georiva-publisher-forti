"""Series computed after the transpose, not before it (D4).

Relative humidity, precipitation windows and 6-hour extremes are all cheap once
the data is a contiguous per-point time series — a few numpy operations over an
array that is already in memory. As derivation-engine recipes they would cost
roughly 600 MB of COGs per run and five more Icechunk repos, to produce map
layers nobody has asked for. If a map layer is ever wanted, promoting one of
these is a separate, additive decision.

Every function here takes and returns ``(time, point)`` float arrays, and the
period ones take the ``Window`` list that ``windows.find_windows`` produced —
never a step count. See ``windows`` for why that distinction is the whole ball
game.
"""

import numpy as np

#: Below this, an accumulation that goes backwards is float noise rather than a
#: variable that is not an accumulation at all. In mm.
ACCUMULATION_TOLERANCE = 0.01


def relative_humidity(temperature_c: np.ndarray, dew_point_c: np.ndarray) -> np.ndarray:
    """Magnus formula, both inputs in degrees Celsius.

    The ratio of saturation vapour pressures at the dew point and at the
    temperature. Clipped to 0–100: the formula can exceed 100 by a fraction of a
    percent when the dew point is reported at or above the temperature, which
    happens in saturated air and is not an error worth failing a publish for.
    """

    def saturation(celsius):
        return np.exp((17.625 * celsius) / (243.04 + celsius))

    return np.clip(100.0 * saturation(dew_point_c) / saturation(temperature_c), 0.0, 100.0)


def accumulation_window(accumulated: np.ndarray, windows) -> np.ndarray:
    """What fell during each window, from a series accumulated since the run start.

    ``end - start``, and specifically **not** ``np.diff(acc, n=hours)``, which is
    the n-th order finite difference — a completely different quantity that
    happens to come out the right length, so `forti-prep`'s assert passes and
    `convert.py:128` has been quietly wrong ever since.

    Raises if the input is not actually an accumulation. A per-step precipitation
    field differenced this way produces plausible small numbers with no sign that
    anything is wrong, and a publication is read by a service that cannot check.
    """
    _assert_accumulating(accumulated)
    return np.stack([accumulated[window.end] - accumulated[window.start] for window in windows])


def window_max(values: np.ndarray, windows) -> np.ndarray:
    """The maximum over each closing window, both endpoints included."""
    return np.stack([values[window.start : window.end + 1].max(axis=0) for window in windows])


def window_min(values: np.ndarray, windows) -> np.ndarray:
    """The minimum over each closing window, both endpoints included."""
    return np.stack([values[window.start : window.end + 1].min(axis=0) for window in windows])


def window_mean(values: np.ndarray, windows) -> np.ndarray:
    """The mean over each closing window, both endpoints included.

    met.no averages cloud cover across a symbol's window before coding it
    (`weather_symbol.py:133`), using a moving mean of ``time_resolution``
    *array elements* — which is the window length in hours only if the data is
    hourly. Ours is not, and its spacing changes mid-run, so the mean is taken
    over the steps the window actually spans.
    """
    return np.stack([values[window.start : window.end + 1].mean(axis=0) for window in windows])


def _assert_accumulating(values: np.ndarray) -> None:
    if values.shape[0] < 2:
        return
    worst = float(np.min(np.diff(values, axis=0)))
    if worst < -ACCUMULATION_TOLERANCE:
        raise ValueError(
            f"Precipitation is not accumulating: it falls by {abs(worst):.3f} mm between "
            f"consecutive steps. Differencing a per-step field over a window produces "
            f"plausible numbers that are silently wrong."
        )
