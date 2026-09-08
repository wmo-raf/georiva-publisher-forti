"""What GeoRiva publishes, under what name, into which locationforecast bucket.

Three vocabularies meet here and none of them can be guessed:

*GeoRiva* names a variable by its slug (``2t``, ``tcc``) and stores it in the
variable's own output unit. *Forti's internal format* names a parameter in
``meta.json`` and knows nothing about what it means — it is an int16 array, a
scale factor and a list of valid times. *locationforecast 2.0* names the field a
consumer finally reads.

The internal names below are **met.no's own production vocabulary**, read out of
`jsonfrontend/cmd/jsonfrontend/jsonformat.json` — the config baked into met.no's
shipped image. We generate that file ourselves, so the names are ours to choose;
matching production means a met.no-shaped config drops straight in. They disagree
with `forti-prep`'s converter, which emits ``precipitation_amount_6h`` and
``air_temperature_max``. Production wins.

## offset is not what the README says

``offset`` is the window's **length in hours**, and ``times`` is the window's
**end**, because jsonfrontend files a value under ``end - offset``
(`encode.go:180`). The jsonfrontend README's example puts ``next_6_hours`` at
``offset: 0``, which collides with ``instant`` — ``ret[duration]`` is keyed by
duration alone. met.no's production config confirms ``0 / 1 / 6 / 12``.

Getting either wrong is silent. The value simply appears under a different
timestep, and every layer involved reports success.

## scale_factor 1.0 is written explicitly

A zero or missing scale factor decodes as **0.1**, not 1 (`values.go:31`). For
the weather symbol that turns code 46 into 4.6 and then into no symbol at all, so
the factor is stated for every parameter and never left to ``omitempty``.

## Units are not converted here

Forti reads units straight out of ``meta.json`` (`encode.go:88`), so what we
publish is whatever the COGs hold. Each parameter therefore declares the GeoRiva
unit symbol it *expects*, and the planner refuses a variable that has been
retuned to something else — a collection restated in kelvin would otherwise
publish plausible-looking nonsense.
"""

from dataclasses import dataclass

#: locationforecast's four buckets. The key is the duration; ``instant`` is the
#: value at the timestep itself.
INSTANT = "instant"
NEXT_1_HOURS = "next_1_hours"
NEXT_6_HOURS = "next_6_hours"
NEXT_12_HOURS = "next_12_hours"


@dataclass(frozen=True)
class Parameter:
    """One series in one publication.

    ``source`` is the GeoRiva variable slug this reads, or ``None`` when the
    series is derived from others post-transpose (D4). ``derivation`` names the
    recipe in ``derivations.py`` for the derived ones.
    """

    name: str
    """Forti's internal name — the key in ``meta.json`` and in jsonformat.json."""

    units: str
    """What goes in ``meta.json``. Read verbatim by jsonfrontend."""

    scale_factor: float
    """int16 = round(value / scale_factor). Always stated; 0 would mean 0.1."""

    group: str
    """Which locationforecast bucket the value lands in."""

    offset: int
    """Window length in hours. 0 for an instant value."""

    locationforecast: str
    """The field name a consumer reads."""

    source: str | None = None
    """GeoRiva variable slug, or None when derived."""

    expects_unit: str | None = None
    """The GeoRiva unit symbol the source variable must carry, if any."""

    derivation: str | None = None
    """Recipe name in ``derivations.py``, for a derived series."""

    summary: bool = False
    """Whether this is locationforecast's ``summary.symbol_code`` rather than a
    value under ``details``."""

    @property
    def is_derived(self) -> bool:
        return self.source is None

    @property
    def is_period(self) -> bool:
        return self.offset > 0


#: Read straight off a COG, one value per timestep.
INSTANT_PARAMETERS = [
    Parameter(
        name="air_temperature_2m",
        units="celsius",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="air_temperature",
        source="2t",
        expects_unit="degC",
    ),
    Parameter(
        name="dew_point_temperature_2m",
        units="celsius",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="dew_point_temperature",
        source="2d",
        expects_unit="degC",
    ),
    Parameter(
        name="air_pressure_at_sea_level",
        units="hPa",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="air_pressure_at_sea_level",
        source="msl",
        expects_unit="hPa",
    ),
    Parameter(
        name="wind_speed_10m",
        units="m/s",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="wind_speed",
        source="wind_speed_10m",
        expects_unit="m/s",
    ),
    Parameter(
        name="wind_from_direction_10m",
        units="degrees",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="wind_from_direction",
        source="wind_dir_10m",
        expects_unit="deg",
    ),
    Parameter(
        name="wind_speed_of_gust",
        units="m/s",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="wind_speed_of_gust",
        source="10fg",
        expects_unit="m/s",
    ),
    Parameter(
        # Already percent out of GeoRiva: paramId 164 is a fraction in the GRIB
        # and the IFS plugin exposes it as %, which is what
        # ``cloud_area_fraction`` reports. Passed through unscaled.
        name="cloud_area_fraction",
        units="%",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="cloud_area_fraction",
        source="tcc",
        expects_unit="%",
    ),
    Parameter(
        name="relative_humidity_2m",
        units="%",
        scale_factor=0.1,
        group=INSTANT,
        offset=0,
        locationforecast="relative_humidity",
        derivation="relative_humidity",
    ),
]


#: Derived over a window that must be *found in the time index*, never taken as a
#: fixed number of steps: ECMWF is 3-hourly to 144 h and 6-hourly beyond, so a
#: 6-hour window is two steps early in a run and one step later on. A window
#: nothing in the run can span is dropped, which is how a 3-hourly feed publishes
#: 6-hour series and no 1-hour ones.
PERIOD_PARAMETERS = [
    Parameter(
        name="precipitation_amount_acc1h",
        units="mm",
        scale_factor=0.1,
        group=NEXT_1_HOURS,
        offset=1,
        locationforecast="precipitation_amount",
        derivation="precipitation_window",
    ),
    Parameter(
        name="weather_symbol",
        units="1",
        scale_factor=1.0,
        group=NEXT_1_HOURS,
        offset=1,
        locationforecast="symbol_code",
        derivation="weather_symbol",
        summary=True,
    ),
    Parameter(
        name="precipitation_amount_acc6h",
        units="mm",
        scale_factor=0.1,
        group=NEXT_6_HOURS,
        offset=6,
        locationforecast="precipitation_amount",
        derivation="precipitation_window",
    ),
    Parameter(
        name="air_temperature_2m_max6h",
        units="celsius",
        scale_factor=0.1,
        group=NEXT_6_HOURS,
        offset=6,
        locationforecast="air_temperature_max",
        derivation="temperature_max",
    ),
    Parameter(
        name="air_temperature_2m_min6h",
        units="celsius",
        scale_factor=0.1,
        group=NEXT_6_HOURS,
        offset=6,
        locationforecast="air_temperature_min",
        derivation="temperature_min",
    ),
    Parameter(
        name="weather_symbol_6h",
        units="1",
        scale_factor=1.0,
        group=NEXT_6_HOURS,
        offset=6,
        locationforecast="symbol_code",
        derivation="weather_symbol",
        summary=True,
    ),
    Parameter(
        name="weather_symbol_12h",
        units="1",
        scale_factor=1.0,
        group=NEXT_12_HOURS,
        offset=12,
        locationforecast="symbol_code",
        derivation="weather_symbol",
        summary=True,
    ),
]

ALL_PARAMETERS = [*INSTANT_PARAMETERS, *PERIOD_PARAMETERS]

BY_NAME = {parameter.name: parameter for parameter in ALL_PARAMETERS}


#: Which GeoRiva variables a derived series reads. Kept beside the table rather
#: than inside ``derivations`` so the planner can answer "can this publication
#: produce that parameter?" without importing numpy.
DERIVATION_INPUTS = {
    "relative_humidity": ("2t", "2d"),
    "precipitation_window": ("tp",),
    "temperature_max": ("2t",),
    "temperature_min": ("2t",),
    "weather_symbol": ("tp", "tcc"),
}


def required_variables(parameters) -> set[str]:
    """Every GeoRiva variable slug these parameters read, directly or through a
    derivation."""
    needed = set()
    for parameter in parameters:
        if parameter.source:
            needed.add(parameter.source)
        if parameter.derivation:
            needed.update(DERIVATION_INPUTS[parameter.derivation])
    return needed


def expected_units() -> dict[str, str]:
    """The GeoRiva unit symbol each source variable must carry.

    Forti copies units out of ``meta.json`` without looking at them, so a
    variable retuned from ``degC`` to ``K`` publishes a number that is wrong by
    273 and labelled celsius, with nothing in the chain to notice. The planner
    checks this map before it reads anything.
    """
    units = {}
    for parameter in ALL_PARAMETERS:
        if parameter.source and parameter.expects_unit:
            units[parameter.source] = parameter.expects_unit
    # The derived series read variables no parameter publishes directly.
    units.setdefault("tp", "mm")
    return units
