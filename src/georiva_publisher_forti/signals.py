"""What makes a publication stale, and what makes it build.

``RunIngestion`` is the right granularity and the only one that ever was. The
alternative — marking a publication stale from ``Asset.post_save`` — fires about
950 times for one ECMWF run (68 files x 14 variables) to produce one publish, and
never once says "the inputs are now whole".

A closed run dispatches immediately rather than waiting for the sweep, because the
whole point of the run record is that this is the moment the data is complete. A
*reopened* run only marks stale: a reopen means more files are still arriving, and
building now would publish a version that the next arrival immediately
supersedes. The sweep picks it up when it closes again.
"""

import logging

from django.dispatch import receiver

from georiva.events.context import context_after, ensure_context, use_context
from georiva.ingestion.domain_signals import run_ingestion_closed, run_ingestion_reopened

logger = logging.getLogger(__name__)


@receiver(run_ingestion_closed, dispatch_uid="forti_publisher_run_closed")
def on_run_closed(sender, run, event=None, **kwargs):
    """A run finished arriving — publish it, as the next link of the run's chain.

    ``event`` is the ``RunIngestionClosed`` core just wrote. Dispatching inside
    the context after it is what puts the run's Correlation in the task headers
    and the close in the publish's Caused-by; the publish task reads them back
    on the far side of the queue.
    """
    from .models import ACTOR, FortiPublication
    from .tasks import dispatch_publish

    publication = FortiPublication.for_collection(run.collection)
    if publication is None:
        return

    publication.mark_stale()
    if event is not None:
        chain = use_context(context_after(event, actor=ACTOR))
    else:
        chain = ensure_context(actor=ACTOR, organisation_id=publication.organisation.pk)
    with chain:
        dispatched = dispatch_publish(publication.pk)
    if not dispatched:
        logger.info(
            "forti: %s already in flight when run %s closed — the running build or the sweep will pick the change up",
            publication.slug,
            run.reference_time,
        )


@receiver(run_ingestion_reopened, dispatch_uid="forti_publisher_run_reopened")
def on_run_reopened(sender, run, **kwargs):
    """More of the run arrived after it closed — mark stale, do not build yet.

    Anything derived from the earlier close is provisional now. Building
    immediately would burn a version on a run still in motion; the revision the
    reopen bumped is what makes the eventual republish outrank it.
    """
    from .models import FortiPublication

    publication = FortiPublication.for_collection(run.collection)
    if publication is not None:
        publication.mark_stale()
