"""Where a publish runs, and why it runs there.

ADR 0025 governs the queues, and this plugin is exactly the kind of work the ADR
was written about. A publish is derived from data that is already published and
already servable — tiles, STAC and EDR all answer without it — so nothing a reader
can observe waits on it. It is **deferrable derived work** and belongs on
``georiva-processing``; the sweep and the retention pass belong on
``georiva-default`` with the other sweeps. Nothing here goes near
``georiva-ingestion``, which runs one pool process and admits fetch and extraction
only.

Every dispatch is ``delay(...)``. ``apply_async(queue=...)`` silently overrides a
task's declaration, which is how two core tasks stayed on the wrong queue for
months; an AST sweep in core's ``test_queue_routing`` keeps it that way.
"""

import logging

from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from georiva.config.celery import app
from georiva.core.build_discipline import build_attempt, dispatch_build, stand_down, sweep_builds

logger = logging.getLogger(__name__)


@app.task(
    name="georiva_publisher_forti.tasks.publish_forti_area",
    bind=True,
    max_retries=0,  # failures go to FAILED; the sweep retries
    acks_late=True,
    queue="georiva-processing",
)
def publish_forti_area(self, publication_id: int, claim: str = "") -> None:
    """Publish one area's latest closed run.

    Always dispatched through ``dispatch_publish``, never called directly: the
    claim is taken when the task is *queued*, and a copy arriving under a claim
    the row no longer holds stands down instead of writing beside its
    replacement.
    """
    from .models import FortiPublication, FortiPublicationBuildLog
    from .planner import NothingToPublish
    from .publisher import publish

    try:
        publication = FortiPublication.objects.select_related(
            "collection",
            "collection__catalog",
            "collection__catalog__organisation",
        ).get(pk=publication_id)
    except FortiPublication.DoesNotExist:
        logger.error("publish_forti_area: publication %d not found", publication_id)
        return

    if stand_down(publication, claim):
        return

    publication.refresh_build_lock(f"celery-{self.request.id or 'unknown'}")

    try:
        with build_attempt(FortiPublicationBuildLog, publication) as facts:
            publish(publication, facts)
    except NothingToPublish as exc:
        # Retrying cannot help until more data arrives, and the sweep would
        # otherwise re-dispatch this every five minutes forever.
        logger.info("publish_forti_area: %s — %s", publication.area_key, exc)
        publication.mark_no_data()
    except Exception as exc:
        logger.exception("publish_forti_area: failed for %s", publication.area_key)
        publication.mark_failed(str(exc))


def dispatch_publish(publication_id: int, claimed_by: str = "signal", force: bool = False) -> bool:
    """Claim one publication and queue its publish; return whether it was queued."""
    from .models import FortiPublication

    return dispatch_build(FortiPublication, publication_id, publish_forti_area, claimed_by, force)


@app.task(
    name="georiva_publisher_forti.tasks.sweep_forti_publications",
    queue="georiva-default",
)
def sweep_forti_publications() -> None:
    """The safety net: recover abandoned claims, then publish anything stale.

    Every publish has a trigger — a run closing — so in normal operation this
    finds nothing. It exists for the cases the trigger cannot cover: a worker that
    died mid-build, a publication created while its run was already closed, a
    task lost between claim and queue.
    """
    from .models import FortiPublication

    sweep_builds(FortiPublication, publish_forti_area)


@app.task(
    name="georiva_publisher_forti.tasks.refresh_forti_jsonformat",
    queue="georiva-default",
)
def refresh_forti_jsonformat() -> None:
    """Rewrite the instance's ``jsonformat.json`` from every publication.

    Separate from the publish because the document spans the instance and a
    publish is one area: writing it inside a publish would make one area's build
    depend on every other area's state, and would rewrite it once per area per
    run.

    One query and one write, never a loop over organisations. The document is
    instance-wide (D20), so one organisation's refresh is the file that governs
    every organisation's serving — and a per-org writer would leave whichever
    organisation refreshed last as the only one whose parameters survive.
    """
    from . import jsonformat
    from .models import FortiPublication

    publications = list(FortiPublication.objects.filter(is_enabled=True))

    try:
        key = jsonformat.publish(publications)
    except Exception:
        logger.exception("refresh_forti_jsonformat: failed")
    else:
        logger.info("refresh_forti_jsonformat: wrote %s", key)


@app.task(
    name="georiva_publisher_forti.tasks.prune_forti_publications",
    bind=True,
    acks_late=True,
    queue="georiva-default",
)
def prune_forti_publications(self) -> None:
    """Daily retention: old versions off the bucket, old build logs out of the DB."""
    from .models import FortiPublication, FortiPublicationBuildLog
    from .publisher import prune

    for publication in FortiPublication.objects.filter(is_enabled=True).select_related(
        "collection__catalog__organisation"
    ):
        started_at = timezone.now()
        try:
            pruned = prune(publication)
        except Exception as exc:
            logger.warning("prune_forti_publications: %s failed: %s", publication.area_key, exc)
            FortiPublicationBuildLog.record(
                publication,
                FortiPublicationBuildLog.Kind.GC,
                FortiPublicationBuildLog.Outcome.FAILURE,
                started_at,
                error=str(exc),
            )
        else:
            if pruned:
                FortiPublicationBuildLog.record(
                    publication,
                    FortiPublicationBuildLog.Kind.GC,
                    FortiPublicationBuildLog.Outcome.SUCCESS,
                    started_at,
                    versions_pruned=pruned,
                )

    expired = FortiPublicationBuildLog.prune_expired()
    if expired:
        logger.info("prune_forti_publications: pruned %d expired build-log row(s)", expired)


def _interval(every: int, period: str) -> IntervalSchedule:
    """An interval schedule, reusing one if it exists.

    Not ``get_or_create``: nothing constrains ``IntervalSchedule`` to be unique on
    ``(every, period)``, and a database that has accumulated duplicates — this dev
    one holds three copies of "every 1 hour" — makes ``get_or_create`` raise
    ``MultipleObjectsReturned`` rather than return one. Registration then fails in
    the handler's ``except`` and the periodic tasks simply never exist, which is a
    quiet way to lose every sweep this plugin has. Any of the duplicates will do.
    """
    existing = IntervalSchedule.objects.filter(every=every, period=period).first()
    return existing or IntervalSchedule.objects.create(every=every, period=period)


@app.on_after_finalize.connect
def setup_forti_periodic_tasks(sender, **kwargs) -> None:
    """Register the sweep, the config refresh and the daily retention pass."""
    schedules = [
        ("sweep_forti_publications", 5, IntervalSchedule.MINUTES),
        ("refresh_forti_jsonformat", 1, IntervalSchedule.HOURS),
        ("prune_forti_publications", 1, IntervalSchedule.DAYS),
    ]

    for name, every, period in schedules:
        # One failing registration must not take the other two with it.
        try:
            PeriodicTask.objects.update_or_create(
                name=f"georiva_publisher_forti.{name}",
                defaults={
                    "task": f"georiva_publisher_forti.tasks.{name}",
                    "interval": _interval(every, period),
                    "enabled": True,
                },
            )
        except Exception as exc:
            logger.warning("Could not register periodic task %s: %s", name, exc)
