"""Two slots for the 10 m wind components, and a row for each on every publication.

Wind can now be given as u and v instead of speed and direction (#49): a store
harvest maps one array to one Variable and runs no vector extraction, so its
collection holds the components only. The slot choices are frozen into the
field, hence the ``AlterField``.

The rows are what :meth:`~.models.FortiPublication.seed_mapping` gives a
publication created from now on, and 0005's docstring says why a slot added
later needs its own migration: seeding reaches only publications created after
it. Filled by the same auto-match, told what each publication already holds —
so a publication reading speed and direction gets two blank rows and keeps its
bytes, and only one with no wind slot set and a collection holding ``10u`` and
``10v`` gets them filled. Its generation is left alone for 0005's reason; a
publication that could not publish before (no wind) is not a change of bytes.
"""

from django.db import migrations, models

from georiva_publisher_forti.mapping import auto_match

NEW_SLOTS = ("10u", "10v")


def seed_component_rows(apps, schema_editor):
    FortiPublication = apps.get_model("georiva_publisher_forti", "FortiPublication")
    FortiVariableMapping = apps.get_model("georiva_publisher_forti", "FortiVariableMapping")

    rows = []
    for publication in FortiPublication.objects.select_related("collection").iterator():
        held = {row.slot: row.variable for row in publication.variable_mappings.select_related("variable")}
        matched = auto_match(publication.collection.variables.all(), mapped=held)
        rows.extend(
            FortiVariableMapping(publication=publication, slot=slot, variable=matched[slot])
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
