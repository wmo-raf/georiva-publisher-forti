"""Help text only — no column changes, and nothing to backfill.

The forecast rule itself is not here: it lives in ``FortiPublication.clean`` and
in the admin's collection chooser, neither of which the database knows about.
What changed on the field is the sentence explaining why a collection might be
refused, and Django includes ``help_text`` in a field's deconstruction, so it
asks for a migration whether or not anything is stored differently.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("georiva_publisher_forti", "0002_fortipublication_slug_and_visibility"),
        ("georivacore", "0015_alter_variable_value_max_alter_variable_value_min"),
    ]

    operations = [
        migrations.AlterField(
            model_name="fortipublication",
            name="collection",
            field=models.OneToOneField(
                help_text=(
                    "The forecast collection this model publishes. A collection that is not a forecast is "
                    "refused — nothing opens a run for one, so there would be nothing to transpose. So is an "
                    "internal collection: it is a derivation intermediate, not a dataset."
                ),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="forti_publication",
                to="georivacore.collection",
            ),
        ),
    ]
