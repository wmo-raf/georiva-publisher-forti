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
    name="georiva_publisher_forti.tasks.refresh_forti_config",
    queue="georiva-default",
)
def refresh_forti_config() -> None:
    """Reconcile both serving configs against what the database says is published.

    The **reconciler**, not the trigger. The trigger is inline at the end of a
    successful publish (`publisher.publish`), because that is the only place
    where "after the markers, and after ``mark_ready``" is a property of the code
    rather than of when a queue happens to run. This is what covers everything a
    publish is not: an area disabled, a publication deleted, a parameter set
    changed by hand, a publish whose inline refresh raised, a worker that died
    between the marker and the config. Event alone is permanently wrong after one
    missed signal (D22).

    Five minutes, aligned with ``sweep_forti_publications`` — but its own task and
    not that task's tail. The sweep can raise before reaching a tail, and this is
    the last line of defence for a serving plane that has already lost its first
    one; putting it downstream of another task's success weakens it exactly when
    things are going wrong. It is also a different job: the sweep dispatches
    builds, this one describes what has already been built.

    One query, both documents, and a write only where the sha differs. Never a
    loop over organisations: the documents are instance-wide (D20/D21), so one
    organisation's refresh writes the files that govern every organisation's
    serving, and a per-org writer would leave whichever organisation refreshed
    last as the only one whose areas survive.
    """
    from . import config

    try:
        written = config.refresh()
    except Exception:
        logger.exception("refresh_forti_config: failed")
    else:
        logger.info("refresh_forti_config: %s", ", ".join(written) if written else "no change")


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


#: The periodic tasks this plugin owns, and the only ones it may delete.
#: Namespaced on the beat row's *name*, which is why the prefix has to be exact.
BEAT_PREFIX = "georiva_publisher_forti."

SCHEDULES = [
    ("sweep_forti_publications", 5, IntervalSchedule.MINUTES),
    ("refresh_forti_config", 5, IntervalSchedule.MINUTES),
    ("prune_forti_publications", 1, IntervalSchedule.DAYS),
]


@app.on_after_finalize.connect
def setup_forti_periodic_tasks(sender, **kwargs) -> None:
    """Register the sweep, the config reconciler and the daily retention pass."""
    for name, every, period in SCHEDULES:
        # One failing registration must not take the other two with it.
        try:
            PeriodicTask.objects.update_or_create(
                name=f"{BEAT_PREFIX}{name}",
                defaults={
                    "task": f"georiva_publisher_forti.tasks.{name}",
                    "interval": _interval(every, period),
                    "enabled": True,
                },
            )
        except Exception as exc:
            logger.warning("Could not register periodic task %s: %s", name, exc)

    _forget_retired_tasks()


def _forget_retired_tasks() -> None:
    """Delete this plugin's beat rows for tasks that no longer exist.

    ``update_or_create`` only ever adds. A task that leaves :data:`SCHEDULES` —
    ``refresh_forti_jsonformat`` did, in this commit — keeps its row, keeps its
    schedule and keeps firing, and every firing is a name no worker has
    registered. Celery's answer to that is to log ``Received unregistered task``
    and reject the message; nothing else says anything, and the row goes on
    looking enabled and healthy in the admin forever.

    Only rows under this plugin's own prefix, and only ones absent from the list
    it just wrote. A beat database is shared with core and with every other
    plugin, and a sweep that deleted by any wider rule would be a plugin
    unregistering somebody else's schedule.
    """
    live = {f"{BEAT_PREFIX}{name}" for name, _, _ in SCHEDULES}

    try:
        retired = PeriodicTask.objects.filter(name__startswith=BEAT_PREFIX).exclude(name__in=live)
        for name in list(retired.values_list("name", flat=True)):
            logger.info("Removing retired periodic task %s", name)
        retired.delete()
    except Exception as exc:
        logger.warning("Could not remove retired periodic tasks: %s", exc)
