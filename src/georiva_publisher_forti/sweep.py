"""What one area key owns on the bucket — the reading both operator commands need.

``rename_forti_model`` reads it to price a rename before it is applied;
``cleanup_forti_orphans`` reads it to say what it is about to delete and then to
delete it. Written once because the two must not be able to disagree: a preview
that undercounts what a later sweep removes is worse than no preview, and the
object they would disagree about is the pointer.

The pointer is the reason this is a function rather than a call to
``sink.list_keys``. ``latest/<area key>`` lives beside the areas and not under
any of them — it has to, because ``rawdataforecaster`` polls one flat directory —
so listing an area's prefix finds every byte it owns *except* the one that makes
those bytes loadable.
"""

from .models import marker_path


def objects_under(sink, area_key: str) -> list[str]:
    """Every object one area key owns, pointer last, relative to the sink root.

    Pointer last because that is the order in which they stop being true: the
    bytes are what a reader loads and the pointer is what sends it there, so a
    listing that led with the pointer would read as the more alarming loss when
    it is the smaller one.

    Appended only where it exists. A publication that has never published has a
    prefix and no pointer, and naming an object that is not there is the one line
    in a preview an operator would act on.
    """
    objects = sorted(sink.list_keys(area_key))
    pointer = marker_path(area_key)
    if sink.exists(pointer):
        objects.append(pointer)
    return objects
