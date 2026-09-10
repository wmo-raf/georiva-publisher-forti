"""One collection published as one Forti area.

A publication is not a copy of a collection — it is the same run, transposed. The
collection stores one COG per variable per timestep, which is what a map tile
wants and the worst possible shape for a point query: ~950 range reads and ~93 MB
across 14 API calls to answer "what is the weather at this coordinate". The same
point out of Forti's layout is one read of about 2 KB.

The unit of publication is an **area**, and an area is one collection. Every
area on the instance lives under **one** prefix, because ``rawdataforecaster``
now filters by area per request and reports which area answered: one process can
hold every organisation's areas, and a prefix per organisation would have meant a
process per organisation (D14/D15).

So the prefix stops being the tenancy boundary. What is left of it here is the
**area key** — ``{org}.{slug}``, one path segment, carrying the owning
organisation in the name rather than in the path. Which tenant a request may be
answered from is now a Django query and a check on every response, not a property
of the storage layout; see ADR 0027's amendment in core.
"""

from django.core.exceptions import ValidationError
from django.db import models

from georiva.core.build_discipline import BuildAttemptLog, BuildDisciplinedModel

#: The one prefix every Forti publication on this instance writes into, and the
#: whole of ``rawdataforecaster``'s ``?prefix=``. It cannot be shadowed by a
#: tenant and cannot be inherited by one registered later: ``_`` is outside the
#: organisation slug grammar, so no organisation can ever be called ``_forti``.
SINK_ROOT = "_forti"

#: Paths under that root whose appearance means "ready". ``latest/<area key>`` is
#: the load trigger — polled every 3 s (`forecast.go:126`) — and **not**
#: ``complete.json``, whatever the internalformat README says. Both are written by
#: the engine, after the bytes, in that order.
MARKER_PATTERNS = ("latest/*", "*/complete.json")


def instance_sink():
    """The sink every Forti publication on this instance shares.

    One function rather than a construction at each call site, because the root
    and the marker rule have to be the same everywhere: a second spelling of
    either is a second grammar, and the reader only understands one.
    """
    from georiva.core.publishing import PublicationSink

    return PublicationSink.instance_wide(SINK_ROOT, marker_patterns=MARKER_PATTERNS)


class FortiPublication(BuildDisciplinedModel):
    """One collection's runs, published as one Forti area.

    Rebuilt when a ``RunIngestion`` closes. The build discipline — the six
    states, the claim at dispatch, the stale-lock recovery — comes from the base;
    everything here is what makes it a *Forti* publication.
    """

    ORGANISATION_LOOKUP = "collection__catalog__organisation"

    collection = models.OneToOneField(
        "georivacore.Collection",
        on_delete=models.CASCADE,
        related_name="forti_publication",
        help_text="The forecast collection this area publishes. Must be public.",
    )

    area = models.SlugField(
        max_length=50,
        help_text=(
            "The area name readers ask for, unique within the organisation. It is "
            "the filename under latest/ and a directory under the prefix, so "
            "changing it strands the old one."
        ),
    )

    is_enabled = models.BooleanField(
        default=True,
        help_text="Disabled publications are skipped by the sweep and dropped from jsonformat.json.",
    )

    # =========================================================================
    # Extent — the points that exist
    # =========================================================================

    west = models.FloatField(help_text="Western edge, degrees east.")
    south = models.FloatField(help_text="Southern edge, degrees north.")
    east = models.FloatField(help_text="Eastern edge, degrees east.")
    north = models.FloatField(help_text="Northern edge, degrees north.")

    # =========================================================================
    # The pinned grid (D9)
    # =========================================================================

    grid_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        editable=False,
        help_text=(
            "MD5 of the coordinate bytes, pinned at the first build. "
            "rawdataforecaster caches its s2 index under this, so a build that "
            "produces a different one is refused rather than published."
        ),
    )
    point_count = models.PositiveIntegerField(default=0, editable=False)

    # =========================================================================
    # What was last published (derived cache)
    # =========================================================================

    published_version = models.BigIntegerField(
        null=True,
        blank=True,
        editable=False,
        help_text="ref_epoch_seconds * 100 + revision — the integer latest/<area> holds.",
    )
    published_reference_time = models.DateTimeField(null=True, blank=True, editable=False)
    published_step_count = models.PositiveIntegerField(default=0, editable=False)
    published_parameters = models.JSONField(
        default=list,
        blank=True,
        editable=False,
        help_text="Forti internal names actually written, which vary with the run's step spacing.",
    )

    # =========================================================================
    # Configuration
    # =========================================================================

    time_until_next_hours = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Override for complete.json's time_until_next. Left blank it is derived "
            "from the gap between the last two closed runs."
        ),
    )

    class Meta:
        ordering = ["collection__slug"]
        verbose_name = "Forti publication"
        indexes = [
            models.Index(fields=["status"], name="idx_forti_pub_status"),
        ]

    def __str__(self):
        return f"{self.area} ← {self.collection.slug} [{self.status}]"

    # =========================================================================
    # Identity
    # =========================================================================

    @property
    def organisation(self):
        return self.collection.catalog.organisation

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    @property
    def area_key(self) -> str:
        """The name this area answers to — everywhere, and to everyone.

        ``{org}.{area}``, and it must stay **one path segment**. ``GetGridInfo``
        (`forti-internalformat/client.go:150`) splits every key on ``/`` and
        skips anything that is not exactly four parts, then reads the third as
        the grid id. ``{org}/{area}/{version}/{grid}/`` is five, so a nested key
        means every grid is skipped and the dataset loads with **no grids and no
        error**. ``forti-internalformat`` is unforked and pinned at v1.0.0, so
        the dot is the fix.

        It is also what keeps an area from colliding with the documents beside
        it under the shared root: every area key contains a ``.``, and
        ``latest``, ``config`` and ``status`` do not.
        """
        return f"{self.organisation.slug}.{self.area}"

    def sink(self):
        """The instance-wide Forti prefix, with the marker rule attached.

        Not this organisation's — every organisation publishes here. What is
        this publication's is :attr:`area_key`, and every path derived below
        starts with it.
        """
        return instance_sink()

    def version_prefix(self, version: int) -> str:
        """Where one version's bytes live, relative to the sink root."""
        return f"{self.area_key}/{version}"

    def marker_path(self) -> str:
        """The pointer a reader polls for this area, relative to the sink root."""
        return f"latest/{self.area_key}"

    # =========================================================================
    # Validation
    # =========================================================================

    def clean(self):
        super().clean()
        errors = {}

        if self.collection_id:
            collection = self.collection
            if collection.visibility != collection.Visibility.PUBLIC:
                errors["collection"] = (
                    "Only public collections may be published. A Forti reader holds no "
                    "credential and cannot be asked who it is, so there is nobody to "
                    "check a private collection against."
                )
            if self.area and self._area_taken(collection):
                errors["area"] = (
                    f"Another publication of this organisation already uses the area "
                    f"name {self.area!r}. Area names are the reader's whole vocabulary: "
                    f"two areas with one name means one of them is unreachable."
                )

        if self.west is not None and self.east is not None and self.west >= self.east:
            errors["east"] = "The eastern edge must be east of the western one."
        if self.south is not None and self.north is not None and self.south >= self.north:
            errors["north"] = "The northern edge must be north of the southern one."

        if errors:
            raise ValidationError(errors)

    def _area_taken(self, collection) -> bool:
        return (
            type(self)
            .objects.filter(
                area=self.area,
                collection__catalog__organisation=collection.catalog.organisation_id,
            )
            .exclude(pk=self.pk)
            .exists()
        )

    def seed_extent_from_catalog(self, margin_degrees: float = 0.5) -> bool:
        """Fill the extent from the catalog's clipping boundary, plus a margin.

        The margin is not decoration: a point forecast for a border town is asked
        for from both sides of the border, and an area that stops exactly at the
        boundary answers "outside coverage" to half of them. Returns whether
        anything was seeded.
        """
        boundary = getattr(self.collection.catalog, "boundary", None)
        geom = getattr(boundary, "geom", None)
        if geom is None:
            return False

        west, south, east, north = geom.extent
        self.west = west - margin_degrees
        self.south = south - margin_degrees
        self.east = east + margin_degrees
        self.north = north + margin_degrees
        return True

    # =========================================================================
    # Queries
    # =========================================================================

    @classmethod
    def get_buildable(cls):
        """Buildable publications, minus the disabled ones.

        Disabling has to remove a publication from the *sweep*, not merely from
        the writer: a disabled publication left PENDING would be claimed and
        dispatched every five minutes forever.
        """
        return (
            super()
            .get_buildable()
            .filter(is_enabled=True)
            .select_related("collection", "collection__catalog", "collection__catalog__organisation")
        )

    @classmethod
    def for_collection(cls, collection):
        """The enabled publication of a collection, or None."""
        return cls.objects.filter(collection=collection, is_enabled=True).first()


class FortiPublicationBuildLog(BuildAttemptLog):
    """One publish attempt, or one retention pass, kept after the fact."""

    TARGET_FIELD = "publication"

    ORGANISATION_LOOKUP = "publication__collection__catalog__organisation"

    publication = models.ForeignKey(
        FortiPublication,
        on_delete=models.CASCADE,
        related_name="build_logs",
    )

    version = models.BigIntegerField(null=True, blank=True)
    step_count = models.PositiveIntegerField(default=0)
    point_count = models.PositiveIntegerField(default=0)
    parameter_count = models.PositiveIntegerField(default=0)
    objects_written = models.PositiveIntegerField(default=0)
    versions_pruned = models.PositiveIntegerField(default=0)

    class Meta(BuildAttemptLog.Meta):
        verbose_name = "Forti publication build log"
        indexes = [
            models.Index(fields=["started_at"], name="idx_forti_log_started"),
        ]

    def __str__(self):
        return f"{self.publication_id} {self.kind}/{self.outcome} @ {self.started_at:%Y-%m-%d %H:%M}"
