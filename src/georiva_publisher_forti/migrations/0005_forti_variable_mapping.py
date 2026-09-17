"""The variable mapping becomes data, and the live publication keeps its bytes.

Which GeoRiva variable fed which Forti parameter was an exact slug match inside
the planner: a collection that named its variables anything else could not
publish, and the only way to discover that was to publish and read the refusal.

The rows created here **describe what is already being served**. They are filled
by the same exact-slug rule the planner used a commit ago — :func:`auto_match`,
imported rather than copied precisely so that the description cannot drift from
the resolution — and the generation is left alone, because nothing about the
bytes has changed. What follows is one byte-identical republish, since the
fingerprint now covers the mapping; the same run under the same mapping produces
the same output.

A publication whose collection does not use the expected names comes out of this
with blank rows rather than with nothing. That is the new state "configured but
not finished", and it is exactly the state such a publication was already in —
it simply had no way to say so.

The ``AlterField`` on ``generation`` is unrelated drift: 0004 was written
without the validator's message, and this is the autodetector catching up.
"""

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models

from georiva_publisher_forti.mapping import auto_match


def seed_from_slug_match(apps, schema_editor):
    """Give every existing publication the eight rows its planner assumed.

    ``bulk_create`` rather than a loop of saves, for the reason the model's own
    seeding uses it: a save raises the generation, and a migration that raised
    it would publish the next run under a stamp claiming to be a correction of
    bytes nobody changed.
    """
    FortiPublication = apps.get_model("georiva_publisher_forti", "FortiPublication")
    FortiVariableMapping = apps.get_model("georiva_publisher_forti", "FortiVariableMapping")

    rows = []
    for publication in FortiPublication.objects.select_related("collection").iterator():
        matched = auto_match(publication.collection.variables.all())
        rows.extend(
            FortiVariableMapping(publication=publication, slot=slot, variable=variable)
            for slot, variable in matched.items()
        )
    FortiVariableMapping.objects.bulk_create(rows)


def drop_mapping(apps, schema_editor):
    """Reversible: before this, resolution was by slug and needed no rows."""
    FortiVariableMapping = apps.get_model("georiva_publisher_forti", "FortiVariableMapping")
    FortiVariableMapping.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('georiva_publisher_forti', '0004_fortipublication_generation'),
        ('georivacore', '0015_alter_variable_value_max_alter_variable_value_min'),
    ]

    operations = [
        migrations.AlterField(
            model_name='fortipublication',
            name='generation',
            field=models.PositiveSmallIntegerField(default=0, help_text='Counts changes to the published bytes that are not changes to the run. rawdataforecaster reloads only on a strictly greater version, so republishing one run under a changed configuration needs a term the run does not supply — otherwise the correct new bytes sit under the stamp the reader already holds and are never loaded. Raise it by one and republish. It resets itself when a new run is published, because a new run at generation 0 already outranks any generation of the one before it.', validators=[django.core.validators.MaxValueValidator(99, message='At most %(limit_value)s. The generation is the low two digits of the published version, so one past this is not a larger number — it is exactly the stamp this run claims at its next revision, and the reader would read the second set of bytes as one it already holds. Wait for the next run, which resets the generation, or publish a second model.')]),
        ),
        migrations.CreateModel(
            name='FortiVariableMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('slot', models.CharField(choices=[('2t', '2t'), ('2d', '2d'), ('msl', 'msl'), ('wind_speed_10m', 'wind_speed_10m'), ('wind_dir_10m', 'wind_dir_10m'), ('10fg', '10fg'), ('tcc', 'tcc'), ('tp', 'tp')], help_text='The role this variable fills in the parameter map.', max_length=32)),
                ('publication', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='variable_mappings', to='georiva_publisher_forti.fortipublication')),
                ('variable', models.ForeignKey(blank=True, help_text='The collection variable that fills this slot. Leave it blank while the collection is still declaring its variables — the publication then reads as not ready rather than as broken.', null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='forti_slots', to='georivacore.variable')),
            ],
            options={
                'verbose_name': 'Forti variable mapping',
                'ordering': ['publication', 'slot'],
                'constraints': [models.UniqueConstraint(fields=('publication', 'slot'), name='unique_forti_slot_per_publication')],
            },
        ),
            migrations.RunPython(seed_from_slug_match, drop_mapping),
    ]
