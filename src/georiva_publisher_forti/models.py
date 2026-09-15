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

That also moves what the name *means*. An area used to describe a **window** —
the first row on the dev instance was called ``kenya`` — and now it names a
**model**: that row is ``ecmwf-ifs``, with Kenya implied by the bbox. One request
names exactly one area and no two are ever blended (D16), so what a consumer
picks between is the model, and ``FortiPublication.slug`` is the name it picks by
(D17). Renaming it is what ``rename_forti_model`` exists for and what
``_published_under_another_slug`` otherwise refuses.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models

from georiva.core.build_discipline import BuildAttemptLog, BuildDisciplinedModel
from georiva.core.models import Collection, visible_visibilities

#: How many generations fit beneath one run revision — the place value the
#: generation occupies in the published version, and therefore also the first
#: generation that has nowhere to go.
#:
#: The two are necessarily the same number, which is why this is one constant and
#: not two. ``run.version * 100 + 100`` is exactly ``(run.version + 1) * 100``:
#: the stamp the run's *next revision* claims at generation 0. A collision by
#: equality is the one the reader cannot see — ``forecast.go:293`` reloads on
#: strictly greater, so it reads the second of two identical stamps as "I already
#: have this" — so publishing there is refused rather than wrapped.
GENERATIONS_PER_REVISION = 100

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


#: How open each tier is. ``internal`` is here because a *collection* can be it,
#: not because a publication can: the choices offer two tiers, so refusing the
#: third is a matter of never having offered it.
_OPENNESS = {"public": 2, "private": 1, "internal": 0, "": 0}

#: Why a collection that is not a forecast can never be published, said in terms
#: of the setting an operator can actually change. The failure it replaces was
#: ``NothingToPublish: has no closed run`` — true, one step downstream of the
#: cause, and recorded in a build log nothing renders.
NOT_A_FORECAST = (
    "Only a forecast collection can be published. Nothing opens a run for a "
    "collection that is not one, and a Forti model publishes one closed run at a "
    "time — a reference time and the steps out from it — so this publication "
    "would have nothing to transpose, ever. Tick 'Is forecast' on the collection "
    "if that is what it holds; otherwise this is not the collection to publish."
)

_RENAME_REFUSED = (
    "This model has published as {stored!r} and cannot be renamed to {wanted!r}. "
    "The slug is a segment of every key already on the bucket: the rename would "
    "leave latest/<org>.{stored} pointing at bytes no retention pass looks at any "
    "more, while readers keep serving the old name and the new one has nothing "
    "under it until the next run. Create a second publication instead."
)


def marker_path(area_key: str) -> str:
    """The pointer a reader polls for one area, relative to the sink root.

    A function as well as :meth:`FortiPublication.marker_path`, because the
    pointer outlives the row: an operator sweeping an area key nothing owns any
    more has the key and no publication to ask. One spelling either way — a
    second is a second grammar, and the reader only understands one.
    """
    return f"latest/{area_key}"


def instance_sink():
    """The sink every Forti publication on this instance shares.

    One function rather than a construction at each call site, because the root
    and the marker rule have to be the same everywhere: a second spelling of
    either is a second grammar, and the reader only understands one.
    """
    from georiva.core.publishing import PublicationSink

    return PublicationSink.instance_wide(SINK_ROOT, marker_patterns=MARKER_PATTERNS)


class FortiPublicationQuerySet(models.QuerySet):
    """Query vocabulary shared by every surface that serves models.

    Written here rather than in the view because the **listing and the detail
    route must not be able to disagree**. D18's rule is that a model the caller
    may not see is absent from ``GET /api/forecast/`` *and* 404s on
    ``GET /api/forecast/{slug}/``, so that the endpoint cannot be used to
    enumerate what a tenant publishes. Two filters written out at two call sites
    is exactly the arrangement in which one of them is later narrowed and the
    other is not — and the failure is silent in the direction that matters: a
    model missing from a listing still answers on its own URL.

    It deliberately mirrors :class:`~georiva.core.models.collection.CollectionQuerySet`
    — ``servable`` / ``public`` / ``visible_to``, in that shape and with those
    names — because a reader who knows what a collection's ``visible_to`` means
    should not have to check whether a publication's means something else.
    """

    def servable(self):
        """Everything this instance could serve to *somebody*.

        Three conditions that travel together and are nothing to do with the
        caller:

        - **enabled**, which is the operator switch;
        - **published**, because ``published_version`` is set only after
          ``latest/<area key>`` is on the bucket. This is the same test
          :func:`rdfconfig.servable` applies when it builds the area list, and
          it has to be applied here too: a model advertised before its first
          publish is one ``rawdataforecaster`` has never been told to load, so
          the request comes back "Outside of coverage area" — a 404 blaming the
          caller's coordinates for a model that has simply never run;
        - the **collection is active and so is its catalog**, which is
          :meth:`CollectionQuerySet.servable`'s other half. A retired collection
          stops being served everywhere else on the instance and must stop being
          served here.

        This set is deliberately a *subset* of what the area list names, never a
        superset: a reader holding an area nothing serves is wasted memory,
        while a route serving an area no reader holds is a 503.
        """
        return self.filter(
            is_enabled=True,
            published_version__isnull=False,
            collection__is_active=True,
            collection__catalog__is_active=True,
        )

    def public(self):
        """The models this instance will serve to anybody who asks.

        Both tiers, because the effective one is the narrower of the two — see
        :attr:`FortiPublication.effective_visibility`. This is the predicate the
        ``Cache-Control: public`` marking is allowed to key on and nothing else:
        the ``/api/`` cache key carries no identity (ADR 0029), so "public" has
        to mean "safe to hand to the next caller, whoever they are".
        """
        return self.servable().filter(
            visibility=FortiPublication.Visibility.PUBLIC,
            collection__visibility=Collection.Visibility.PUBLIC,
        )

    def visible_to(self, request):
        """The models ``request`` may be served, by tier and by audience.

        The audience rule is core's, imported rather than restated:
        :func:`~georiva.core.models.visible_visibilities` asks
        ``organisations.access.may_see_private``, which is the same function the
        STAC API and the dataset pages ask. A caller who may not see a private
        model gets no 403 out of this — the model is simply absent, so the
        listing omits it and a fetch by name 404s (#273).

        **Both tiers are filtered, the publication's and the collection's.**
        ``clean()`` already refuses a publication more open than its collection,
        but validation runs when the *publication* is saved and the collection
        can be narrowed afterwards — by an editor who has no reason to know a
        Forti publication exists. Reading the pair means the invariant holds at
        the moment it is used rather than at the moment it was last checked, and
        it is what makes ``internal`` unservable here without naming it: no tier
        this returns ever includes it.

        Says nothing about *whose* rows these are. The organisation filter stays
        at the call site, wrapped in ``scoped_queryset`` (ADR 0011).
        """
        tiers = visible_visibilities(request)
        return self.servable().filter(visibility__in=tiers, collection__visibility__in=tiers)


class FortiPublication(BuildDisciplinedModel):
    """One collection's runs, published as one Forti area.

    Rebuilt when a ``RunIngestion`` closes. The build discipline — the six
    states, the claim at dispatch, the stale-lock recovery — comes from the base;
    everything here is what makes it a *Forti* publication.
    """

    objects = FortiPublicationQuerySet.as_manager()

    ORGANISATION_LOOKUP = "collection__catalog__organisation"

    collection = models.OneToOneField(
        "georivacore.Collection",
        on_delete=models.CASCADE,
        related_name="forti_publication",
        help_text=(
            "The forecast collection this model publishes. A collection that is "
            "not a forecast is refused — nothing opens a run for one, so there "
            "would be nothing to transpose. So is an internal collection: it is "
            "a derivation intermediate, not a dataset."
        ),
    )

    slug = models.SlugField(
        max_length=50,
        blank=True,
        help_text=(
            "The model name a consumer asks for: GET /api/forecast/{slug}/. "
            "Unique within the organisation, in the same grammar as the catalog "
            "slug it is prefilled from — leave it blank to take that. Immutable "
            "once published: it is a segment of every storage key."
        ),
    )

    class Visibility(models.TextChoices):
        PUBLIC = "public", "Public"
        PRIVATE = "private", "Private"

    visibility = models.CharField(
        max_length=10,
        choices=Visibility.choices,
        blank=True,
        default="",
        help_text=(
            "Who may ask for this model. Blank takes the collection's, which is "
            "the usual answer; it may be narrowed from there but never widened "
            "past it. A caller who may not see a model finds it absent from the "
            "listing and 404s on it directly, so the endpoint cannot be used to "
            "enumerate what a tenant publishes."
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
        help_text=(
            "run.version * 100 + generation — the integer latest/<area key> "
            "holds. The run's own half is ref_epoch_seconds * 100 + revision, "
            "so the whole stamp orders by model time, then by republish of that "
            "run, then by configuration generation."
        ),
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

    generation = models.PositiveSmallIntegerField(
        default=0,
        validators=[
            MaxValueValidator(
                GENERATIONS_PER_REVISION - 1,
                message=(
                    "At most %(limit_value)s. The generation is the low two digits of the "
                    "published version, so one past this is not a larger number — it is "
                    "exactly the stamp this run claims at its next revision, and the reader "
                    "would read the second set of bytes as one it already holds. Wait for "
                    "the next run, which resets the generation, or publish a second model."
                ),
            )
        ],
        help_text=(
            "Counts changes to the published bytes that are not changes to the "
            "run. rawdataforecaster reloads only on a strictly greater version, "
            "so republishing one run under a changed configuration needs a term "
            "the run does not supply — otherwise the correct new bytes sit under "
            "the stamp the reader already holds and are never loaded. Raise it "
            "by one and republish. It resets itself when a new run is published, "
            "because a new run at generation 0 already outranks any generation of "
            "the one before it."
        ),
    )

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
        return f"{self.slug} ← {self.collection.slug} [{self.status}]"

    # =========================================================================
    # Identity
    # =========================================================================

    @property
    def organisation(self):
        return self.collection.catalog.organisation

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    def stored_generation_for(self, run_version: int) -> int:
        """This row's generation as the *database* has it, reset if the run moved on.

        Read from the database rather than from this instance, for the same
        reason :meth:`_published_under_another_slug` does and on the same field:
        the build discipline writes every transition with a queryset ``update()``,
        so an in-memory instance's idea of ``published_version`` is routinely
        stale by exactly the transition that matters. A stale caller here would
        see no publish, reset to 0, and move the pointer *backwards* — which is
        the failure this whole mechanism exists to prevent.

        Which run the generation was raised against is recovered from the stamp
        by integer division rather than kept in a column of its own. A second
        column would be a second authority on one fact, and the two would part
        company — most plausibly when a publish died between the two writes,
        which is precisely when an operator is reading them to work out what
        happened.

        Rows published by an older version of this plugin hold a *run* version in
        ``published_version``, not a publication one, so the division yields
        roughly the reference time's epoch seconds — a number no live run's
        version equals, and the generation resets. That is the right answer for
        an unknown predecessor, and the new stamp is ~100x the old one, so the
        pointer still only ever moves forwards.
        """
        stored = type(self).objects.filter(pk=self.pk).values("published_version", "generation").first()
        if stored is None or stored["published_version"] is None:
            return 0
        if stored["published_version"] // GENERATIONS_PER_REVISION != run_version:
            return 0
        return stored["generation"]

    @property
    def effective_visibility(self) -> str:
        """The tier this model is actually served at: the narrower of the two.

        ``clean()`` refuses a publication more open than its collection, but it
        runs when the *publication* is saved. Narrowing the collection
        afterwards is an ordinary edit made by somebody who need not know a
        Forti publication exists, and it must not leave a stored ``public`` on
        this row serving what the rest of the instance has stopped serving.

        The read side of that is :meth:`FortiPublicationQuerySet.visible_to`,
        which filters both tiers and so returns exactly the rows whose effective
        tier admits the caller. This is the same answer for one row in hand —
        used where a query cannot be: deciding whether a *response* may carry
        ``Cache-Control: public``, and saying which tier the listing reports.
        """
        collection_visibility = self.collection.visibility
        if _OPENNESS[self.visibility] <= _OPENNESS[collection_visibility]:
            return self.visibility
        return collection_visibility

    @property
    def area_key(self) -> str:
        """The name this area answers to — everywhere, and to everyone.

        ``{org}.{slug}``, and it must stay **one path segment**. ``GetGridInfo``
        (`forti-internalformat/client.go:150`) splits every key on ``/`` and
        skips anything that is not exactly four parts, then reads the third as
        the grid id. ``{org}/{slug}/{version}/{grid}/`` is five, so a nested key
        means every grid is skipped and the dataset loads with **no grids and no
        error**. ``forti-internalformat`` is unforked and pinned at v1.0.0, so
        the dot is the fix.

        It is also what keeps an area from colliding with the documents beside
        it under the shared root: every area key contains a ``.``, and
        ``latest``, ``config`` and ``status`` do not.
        """
        return f"{self.organisation.slug}.{self.slug}"

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
        # The module-level function, which is where the grammar lives; a bare
        # name in a method body resolves to the global, not to this method.
        return marker_path(self.area_key)

    # =========================================================================
    # Validation
    # =========================================================================

    def clean(self):
        super().clean()
        self.inherit_from_collection()
        errors = {}

        if self.collection_id:
            collection = self.collection
            # Chained ahead of the visibility pair because it is the further-back
            # fact: a collection with no runs has nothing to be visible *of*.
            if not collection.is_forecast:
                errors["collection"] = NOT_A_FORECAST
            elif collection.visibility == collection.Visibility.INTERNAL:
                errors["collection"] = (
                    "An internal collection cannot be published. It is a derivation "
                    "intermediate read by the engine as an input — not a dataset with "
                    "a small audience, and there is no audience to narrow it to."
                )
            elif _OPENNESS[self.visibility] > _OPENNESS[collection.visibility]:
                errors["visibility"] = (
                    f"A {self.get_visibility_display().lower()} model over a "
                    f"{collection.get_visibility_display().lower()} collection would serve "
                    f"through Forti what the collection is not served through anywhere "
                    f"else. Visibility may be narrowed from the collection's, never widened."
                )

            if self.slug and self._slug_taken(collection):
                errors["slug"] = (
                    f"Another publication of this organisation is already the model "
                    f"{self.slug!r}. The slug is the name a consumer asks by and a "
                    f"segment of every storage key: two models with one name means one "
                    f"of them is unreachable."
                )

        renamed = self._published_under_another_slug()
        if renamed is not None:
            errors["slug"] = _RENAME_REFUSED.format(stored=renamed, wanted=self.slug)

        if self.west is not None and self.east is not None and self.west >= self.east:
            errors["east"] = "The eastern edge must be east of the western one."
        if self.south is not None and self.north is not None and self.south >= self.north:
            errors["north"] = "The northern edge must be north of the southern one."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Enforced here as well as in ``clean()``, for the two that are not
        validation niceties.

        A slug change after a publish is a storage fact, exactly as
        ``Organisation.slug`` is: keys already carry the old one. And a blank
        visibility is a *form* state, never a stored one — a row that reached the
        database without a tier would be missing from every serving query, which
        reads as "no such model" rather than as anything having gone wrong. Code
        paths that skip ``full_clean()`` must not be able to produce either.
        """
        self.inherit_from_collection()

        renamed = self._published_under_another_slug()
        if renamed is not None:
            raise ValidationError(
                {"slug": _RENAME_REFUSED.format(stored=renamed, wanted=self.slug)},
                code="immutable_published_slug",
            )
        return super().save(*args, **kwargs)

    def inherit_from_collection(self) -> None:
        """Fill what the collection decides, where nothing else has decided it.

        The slug is prefilled from ``catalog.slug`` because that is already the
        right name and the right scope: ``Catalog``'s own docstring calls it *"a
        data source that produces multiple collections. Examples: GFS, CHIRPS,
        ERA5, MSG"*, and its slug is unique per organisation, which is exactly
        what a public model name needs to be.

        Visibility defaults from the collection because deriving it is right
        almost always, and the field exists for the case it is not: an NMHS may
        reasonably serve maps publicly and point forecasts to members only, which
        deriving cannot express in either direction.
        """
        if (self.slug and self.visibility) or not self.collection_id:
            return

        collection = self.collection
        if not self.slug:
            self.slug = collection.catalog.slug
        if not self.visibility:
            # An internal collection lands here as an unspellable tier. That is
            # the safe direction — no serving query matches it — and ``clean()``
            # refuses the collection outright.
            self.visibility = collection.visibility

    def _slug_taken(self, collection) -> bool:
        return (
            type(self)
            .objects.filter(
                slug=self.slug,
                collection__catalog__organisation=collection.catalog.organisation_id,
            )
            .exclude(pk=self.pk)
            .exists()
        )

    def _published_under_another_slug(self) -> str | None:
        """The stored slug, if this row has published and is being renamed.

        Read from the database rather than from ``__init__``'s copy: the build
        discipline writes every transition with a queryset ``update()``, so an
        in-memory instance's idea of ``published_version`` is routinely stale by
        exactly the transition that matters.
        """
        if not self.pk:
            return None

        stored = type(self).objects.filter(pk=self.pk).values("slug", "published_version").first()
        if stored is None or stored["published_version"] is None:
            return None
        if stored["slug"] == self.slug:
            return None
        return stored["slug"]

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
