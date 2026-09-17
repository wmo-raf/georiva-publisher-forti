"""The rule that fills a slot when nobody has said otherwise.

Until the mapping became data, which GeoRiva variable fed which Forti parameter
was an exact slug match buried in the planner: a collection had to call its
2-metre temperature ``2t`` or it could not publish at all. That rule is not
wrong — it is right for every collection this plugin has ever published — so it
survives as the **default**, and what changed is that it is now overridable.

Kept in a module of its own, pure, and importing nothing from Django, because a
data migration imports it. A migration that reached into ``models`` would
rewrite its own history the next time a model changed; one that duplicated the
rule would describe the bytes a publication is serving using a rule that is no
longer the one that produced them.
"""

from . import parameters as params


def auto_match(variables) -> dict:
    """``{slot key: variable or None}`` — every slot answered, by slug.

    Takes anything with a ``.slug``, so a historical model in a migration and a
    real ``Variable`` both work. Every slot appears in the result whether or not
    a variable fills it: a blank slot is a thing a publication *has*, and being
    asked to fill one in is how a collection that names its variables
    differently becomes publishable at all.
    """
    by_slug = {variable.slug: variable for variable in variables}
    return {key: by_slug.get(key) for key in params.SLOT_KEYS}
