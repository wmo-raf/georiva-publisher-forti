"""What GeoRiva publishes, under what name, into which locationforecast bucket.

Three vocabularies meet here and none of them can be guessed:

*GeoRiva* names a variable by its slug (``2t``, ``tcc``) and stores it in the
variable's source unit, converting it when it is read (ADR 0046). *Forti's internal format* names a parameter in
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

## Units are converted on read, to the unit each parameter declares

Forti reads units straight out of ``meta.json`` (`encode.go:88`) without
interpreting them, so what we publish must be in exactly the unit we label it.
Each parameter therefore declares the GeoRiva unit symbol it *expects*, and the
publisher reads its slot in that unit through core's read module, which converts
from the variable's source unit — kelvin is read as celsius. A variable whose
source unit cannot be converted to it (a pressure in a temperature slot, a mass
per area not flagged as water) is refused.
"""

from dataclasses import dataclass

from . import derivations
from .prose import listed

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
    """The GeoRiva unit symbol the source variable is read in. Its source unit
    must convert to it."""

    derivation: str | None = None
    """Recipe name in ``derivations.py``, for a derived series."""

    summary: bool = False
    """Whether this is locationforecast's ``summary.symbol_code`` rather than a
    value under ``details``."""

    optional: bool = False
    """Whether a publication may go without it. An optional parameter whose slot
    is blank is left out of ``meta.json`` — which a reader sees as the field
    being absent, and locationforecast treats every ``details`` field as
    optional. For a quantity some models simply do not produce; never for one
    a derivation reads."""

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
        # Not every model has one — WRF does not write it by default. Nothing
        # derives from it, so leaving it out spoils no other series.
        optional=True,
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

#: The unit expected of a slot no parameter publishes directly. ``tp`` is read
#: by four derivations and written by none, so no ``Parameter`` above declares
#: its unit — and every slot is read in a declared unit, so every slot must have one.
DERIVED_INPUT_UNITS = {
    "tp": "mm",
    "10u": "m/s",
    "10v": "m/s",
}


@dataclass(frozen=True)
class Way:
    """One complete set of slots that gives a quantity."""

    label: str
    """What an operator calls this way, in a sentence."""

    keys: tuple[str, ...]


@dataclass(frozen=True)
class Alternative:
    """A quantity a publication gives one complete way out of several.

    Wind, which a collection holds either as speed and direction — core
    computes those from the components at ingestion — or as the components
    alone, as a store harvest does: it maps one array to one Variable and runs
    no vector extraction. Each way is complete on its own, and a mapping must
    use exactly one: a slot of one way beside a slot of the other is two
    answers to one question, and which one is published is not this plugin's to
    guess. So no slot of an alternative is required on its own, and none is
    optional either.
    """

    name: str
    ways: tuple[Way, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(key for way in self.ways for key in way.keys)

    def gap(self, filled: set) -> "Gap | None":
        """What stops this alternative resolving with these slots filled, if anything."""
        touched = [way for way in self.ways if any(key in filled for key in way.keys)]
        either = " or ".join(f"{listed(way.keys)} ({way.label})" for way in self.ways)

        if not touched:
            return Gap(
                alternative=self.name,
                missing=self.keys,
                message=f"{self.name.capitalize()} is not set. Set {either}.",
            )

        if len(touched) == 1:
            (way,) = touched
            missing = tuple(key for key in way.keys if key not in filled)
            if not missing:
                return None
            have = [key for key in way.keys if key in filled]
            return Gap(
                alternative=self.name,
                missing=missing,
                message=(
                    f"{self.name.capitalize()} is incomplete: {listed(missing)} is not set, and "
                    f"{listed(have)} needs it. Set {either}."
                ),
            )

        set_per_way = " and ".join(
            f"{listed([key for key in way.keys if key in filled])} ({way.label})" for way in touched
        )
        remedies = []
        for way in self.ways:
            missing = [key for key in way.keys if key not in filled]
            others = [key for other in self.ways if other is not way for key in other.keys if key in filled]
            needed = f"set {listed(missing)} and clear {listed(others)}" if missing else f"clear {listed(others)}"
            remedies.append(f"for {way.label}, {needed}")
        return Gap(
            alternative=self.name,
            missing=(),
            message=(
                f"{self.name.capitalize()} mixes two ways of giving it: {set_per_way}. Use one — "
                f"{'; or '.join(remedies)}."
            ),
        )


@dataclass(frozen=True)
class Gap:
    """Why an alternative does not resolve, said once for every surface that says it."""

    alternative: str
    missing: tuple[str, ...]
    """The slots a row should read as not set. Empty for a mix, which is not a
    blank row but two filled ones that disagree."""

    message: str


WIND = Alternative(
    name="wind",
    ways=(
        Way(label="speed and direction", keys=("wind_speed_10m", "wind_dir_10m")),
        Way(label="u and v components", keys=("10u", "10v")),
    ),
)

#: Every quantity given one way out of several. The first way of each is the
#: one the parameter map reads directly; the others stand in for it through
#: :data:`SOURCE_DERIVATIONS`.
ALTERNATIVES = (WIND,)

#: A parameter's source, derived after the transpose from other slots when the
#: publication fills those instead: ``{source slot: (recipe, input slots)}``.
#: Speed is the vector magnitude; direction is the bearing the wind comes from,
#: as core's vector-direction transform computes it at ingestion.
SOURCE_DERIVATIONS = {
    "wind_speed_10m": (derivations.wind_speed, ("10u", "10v")),
    "wind_dir_10m": (derivations.wind_from_direction, ("10u", "10v")),
}


def unresolved_alternatives(filled) -> tuple[Gap, ...]:
    """Every alternative these filled slots do not resolve to exactly one way.

    Keys only, so the planner's refusal, the readiness finding and the mapping
    page all ask the same function and say the same sentence.
    """
    filled = set(filled)
    return tuple(gap for gap in (alternative.gap(filled) for alternative in ALTERNATIVES) if gap is not None)


#: What each slot's values plausibly span, in the slot's own unit — generous on
#: purpose, because this is the loose half of a pair whose other half is exact.
#:
#: The unit check converts kelvin to celsius and refuses a pressure. It cannot see
#: a mislabelled variable: one whose source unit *says* ``degC`` while its
#: numbers are kelvin is read as celsius and publishes 297 degrees. What is
#: left to notice it with is the variable's declared range, which core documents
#: as a styling hint — "minimum expected data value … used for color mapping" —
#: and which is therefore evidence rather than proof. So the bounds below are
#: drawn wide enough that only a range sharing *no value at all* with them is
#: remarkable, and the comparison that reads them (:func:`~.mapping.concerns`)
#: warns rather than refuses.
#:
#: Declared for every slot, and checked below, for the same reason the unit is:
#: a slot silently missing one is not loudly wrong, it is quietly unsuspectable.
PLAUSIBLE_RANGES = {
    # Vostok read −89.2 °C and Furnace Creek 56.7 °C; a forecast field is
    # narrower than either, and neither margin costs anything.
    "2t": (-90.0, 60.0),
    # Dew point cannot exceed the air temperature it accompanies, and the warm
    # end of it is bounded by how much water air can hold.
    "2d": (-90.0, 40.0),
    # 870 hPa in the eye of Tip, 1084 hPa over Siberia — both at sea level,
    # which is what this slot reduces to.
    "msl": (850.0, 1100.0),
    # A sustained surface wind; the gust slot below is given more room.
    "wind_speed_10m": (0.0, 120.0),
    # A compass bearing, whole circle.
    "wind_dir_10m": (0.0, 360.0),
    # A signed component of the sustained wind above, either way.
    "10u": (-120.0, 120.0),
    "10v": (-120.0, 120.0),
    "10fg": (0.0, 150.0),
    # A fraction expressed as a percentage, already scaled by the source plugin.
    "tcc": (0.0, 100.0),
    # Accumulated from the start of the run, so the ceiling is a run's total
    # rather than a timestep's.
    "tp": (0.0, 2000.0),
}


@dataclass(frozen=True)
class Slot:
    """One GeoRiva variable a publication has to name.

    A slot is the *role*, not the variable: "whatever this collection calls
    2-metre temperature". Eight of the fifteen parameters are derived after the
    transpose and read no variable of their own, so the mappable set is not the
    parameter list — it is the variables the parameter map reads, directly or
    through a derivation.

    The set is fixed and belongs to the format, which is why it is derived from
    the table above rather than typed out beside it: a parameter added with a
    new ``source`` becomes a slot in the same commit, instead of a source
    nothing can be mapped to.
    """

    key: str
    """The GeoRiva slug this role is named by — and what auto-match looks for."""

    units: str
    """The unit symbol the slot is read in. A variable whose source unit cannot
    be converted to it is refused."""

    feeds: tuple[str, ...]
    """The Forti parameters that read it, so a surface can say what a wrong
    mapping would spoil without knowing the parameter map itself."""

    plausible: tuple[float, float]
    """The span its values plausibly occupy, in :attr:`units`. A suspicion, not
    a rule — see :data:`PLAUSIBLE_RANGES`."""

    optional: bool = False
    """Whether a publication may leave it blank. True only when every parameter
    that reads the slot is itself optional — a slot a required series or a
    derivation needs is required, whatever else reads it."""

    alternative: str | None = None
    """The :class:`Alternative` this slot is one way of giving, if any. Such a
    slot is neither required nor optional on its own: the alternative is
    required, as one complete way."""

    @property
    def required(self) -> bool:
        """Whether a publication cannot publish with this slot blank, on its own.

        The one question every surface asks of a slot: the planner's blank-slot
        refusal, the readiness count and the mapping page's blank rows. A slot
        of an alternative is not — whether it is needed is the alternative's
        to say (:func:`unresolved_alternatives`) — and neither is an optional one."""
        return not self.optional and self.alternative is None


def _build_slots() -> tuple[Slot, ...]:
    """The vocabulary, in the order the parameter map first reads each slot.

    Order is part of the vocabulary: it is what a mapping surface renders, and
    reading order puts the seven directly-published slots first and ``tp``,
    which only derivations read, last.
    """
    feeds: dict[str, list[str]] = {}
    units: dict[str, str] = {}
    needed: set[str] = set()

    for parameter in ALL_PARAMETERS:
        keys = [parameter.source] if parameter.source else []
        if parameter.derivation:
            keys.extend(DERIVATION_INPUTS[parameter.derivation])
        for key in keys:
            feeds.setdefault(key, [])
            if not parameter.optional:
                needed.add(key)
            if parameter.name not in feeds[key]:
                feeds[key].append(parameter.name)
        if parameter.source and parameter.expects_unit:
            units[parameter.source] = parameter.expects_unit

    # A slot standing in for a source, read by the parameters that source
    # feeds and placed straight after the last slot of the way it stands in for.
    for source, (_recipe, inputs) in SOURCE_DERIVATIONS.items():
        for key in inputs:
            feeds.setdefault(key, [])
            for name in feeds[source]:
                if name not in feeds[key]:
                    feeds[key].append(name)
    order = [key for key in feeds if not any(key in way.keys for a in ALTERNATIVES for way in a.ways[1:])]
    for alternative in ALTERNATIVES:
        after = max(order.index(key) for key in alternative.ways[0].keys)
        stand_ins = [key for way in alternative.ways[1:] for key in way.keys]
        order[after + 1 : after + 1] = stand_ins
    alternative_of = {key: alternative.name for alternative in ALTERNATIVES for key in alternative.keys}

    units.update(DERIVED_INPUT_UNITS)

    undeclared = [key for key in feeds if key not in units]
    if undeclared:
        raise RuntimeError(
            f"Slot(s) {', '.join(sorted(undeclared))} declare no expected unit. Forti copies "
            f"units out of meta.json without interpreting them, so every slot is read in a "
            f"declared unit; one without would publish whatever unit its variable is served in. "
            f"Give the parameter an expects_unit, or add the slot to DERIVED_INPUT_UNITS."
        )

    unbounded = [key for key in feeds if key not in PLAUSIBLE_RANGES]
    if unbounded:
        raise RuntimeError(
            f"Slot(s) {', '.join(sorted(unbounded))} declare no plausible range. The unit check "
            f"does not see a variable whose unit row says degC over numbers that are kelvin, and "
            f"the declared range is the only thing that would — so a slot without one is not "
            f"loudly unchecked, it is quietly unsuspectable. Add it to PLAUSIBLE_RANGES."
        )

    return tuple(
        Slot(
            key=key,
            units=units[key],
            feeds=tuple(names),
            plausible=PLAUSIBLE_RANGES[key],
            optional=key not in needed and key not in alternative_of,
            alternative=alternative_of.get(key),
        )
        for key, names in ((key, feeds[key]) for key in order)
    )


#: The ten variables a publication maps — wind two ways. Fixed, and defined here only.
SLOTS = _build_slots()

BY_SLOT = {slot.key: slot for slot in SLOTS}

SLOT_KEYS = tuple(slot.key for slot in SLOTS)

#: The slots a publication cannot publish without, each on its own. The rest
#: are optional, or one way of giving an :data:`ALTERNATIVES` quantity, which is
#: required as a whole — see :func:`unresolved_alternatives`.
REQUIRED_SLOT_KEYS = tuple(slot.key for slot in SLOTS if slot.required)


def required_for(filled) -> tuple[str, ...]:
    """The slots a publication with these filled must have, in vocabulary order.

    The required slots, and for each alternative the way it is closest to
    giving — the one with most slots filled, the first on a tie. What the
    readiness count is out of: a publication giving wind either way reads as
    complete against the same number.
    """
    filled = set(filled)
    keys = set(REQUIRED_SLOT_KEYS)
    for alternative in ALTERNATIVES:
        way = max(alternative.ways, key=lambda way: sum(key in filled for key in way.keys))
        keys.update(way.keys)
    return tuple(key for key in SLOT_KEYS if key in keys)


def parameters_for(filled) -> list[Parameter]:
    """The parameters a publication with these slots filled can publish.

    Everything, less the optional series whose slot is blank. A required slot
    is never blank here — the planner refuses that first — so what drops out is
    only ever an optional one. A source a publication gives through other slots
    (wind through its components, :data:`SOURCE_DERIVATIONS`) is filled.
    """
    filled = set(filled)
    return [parameter for parameter in ALL_PARAMETERS if not parameter.source or _given(parameter.source, filled)]


def _given(source: str, filled: set) -> bool:
    """Whether a source is filled, or derivable from slots that are."""
    if source in filled:
        return True
    derivation = SOURCE_DERIVATIONS.get(source)
    return derivation is not None and set(derivation[1]) <= filled


#: For the model field. A literal list rather than the tuple above, because
#: ``choices`` is frozen into a migration and a tuple would churn it.
SLOT_CHOICES = [(slot.key, slot.key) for slot in SLOTS]
