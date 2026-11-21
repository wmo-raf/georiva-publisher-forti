"""Two slots for the 10 m wind components, and a row for each on every publication.

Wind can now be given as u and v instead of speed and direction (#49): a store
harvest maps one array to one Variable and runs no vector extraction, so its
collection holds the components only. The slot choices are frozen into the
field, hence the ``AlterField``.

The rows are what :meth:`~.models.FortiPublication.seed_mapping` gives a
publication created from now on, and 0005's docstring says why a slot added
later needs its own migration: seeding reaches only publications created after
it. Filled by the auto-match rule as it stood when this was written, told what
each publication already holds — so a publication reading speed and direction
gets two blank rows and keeps its bytes, and only one with no wind slot set and
a collection holding ``10u`` and ``10v`` (and neither ``wind_speed_10m`` nor
``wind_dir_10m``) gets them filled. Its generation is left alone for 0005's
reason; a publication that could not publish before (no wind) is not a change of
bytes.

The rule is copied here, on historical models, rather than imported from
``mapping``: what this migration wrote is history, and a later change to the
live auto-match must not change it.
"""

from django.db import migrations, models

SPEED_AND_DIRECTION = ("wind_speed_10m", "wind_dir_10m")
NEW_SLOTS = ("10u", "10v")


def _components_are_the_way(held: dict, slugs: set) -> bool:
    """Whether wind is given as u and v: the way a held row fills wins, and with
    none filled, the first way the collection has a slug of — speed and
    direction before the components."""
    for way in (SPEED_AND_DIRECTION, NEW_SLOTS):
        if any(held.get(key) is not None for key in way):
            return way == NEW_SLOTS
    return not any(key in slugs for key in SPEED_AND_DIRECTION) and any(key in slugs for key in NEW_SLOTS)


def seed_component_rows(apps, schema_editor):
    FortiPublication = apps.get_model("georiva_publisher_forti", "FortiPublication")
    FortiVariableMapping = apps.get_model("georiva_publisher_forti", "FortiVariableMapping")

    rows = []
    for publication in FortiPublication.objects.select_related("collection").iterator():
        held = {row.slot: row.variable_id for row in publication.variable_mappings.all()}
        by_slug = {variable.slug: variable for variable in publication.collection.variables.all()}
        components = _components_are_the_way(held, set(by_slug))
        rows.extend(
            FortiVariableMapping(
                publication=publication, slot=slot, variable=by_slug.get(slot) if components else None
            )
            for slot in NEW_SLOTS
            if slot not in held
        )
    FortiVariableMapping.objects.bulk_create(rows)


def drop_component_rows(apps, schema_editor):
    FortiVariableMapping = apps.get_model("georiva_publisher_forti", "FortiVariableMapping")
    FortiVariableMapping.objects.filter(slot__in=NEW_SLOTS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("georiva_publisher_forti", "0007_publication_written"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fortivariablemapping",
            name="slot",
            field=models.CharField(
                choices=[
                    ("2t", "2t"),
                    ("2d", "2d"),
                    ("msl", "msl"),
                    ("wind_speed_10m", "wind_speed_10m"),
                    ("wind_dir_10m", "wind_dir_10m"),
                    ("10u", "10u"),
                    ("10v", "10v"),
                    ("10fg", "10fg"),
                    ("tcc", "tcc"),
                    ("tp", "tp"),
                ],
                help_text="The Forti parameter this variable is published as.",
                max_length=32,
                verbose_name="Forti parameter",
            ),
        ),
        migrations.RunPython(seed_component_rows, drop_component_rows),
    ]
