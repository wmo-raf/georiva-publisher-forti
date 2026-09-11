"""The area becomes a model: a slug, and who may ask for it.

A **rename**, not a drop and an add. Django's non-interactive autodetector
proposes ``RemoveField('area')`` + ``AddField('slug')``, which discards every
row's name — and the name is a segment of every key already on the bucket, so
the rows it silently emptied would republish under a different prefix and strand
what they had published.

``visibility`` is backfilled from each publication's own collection rather than
defaulted to a constant. A blank tier is a form state meaning "take the
collection's", and a row that reached the database without one would be missing
from every serving query — which reads as "no such model" rather than as
anything having gone wrong.
"""

import django.db.models.deletion
from django.db import migrations, models


def inherit_visibility_from_collection(apps, schema_editor):
    FortiPublication = apps.get_model("georiva_publisher_forti", "FortiPublication")

    for publication in FortiPublication.objects.select_related("collection").iterator():
        publication.visibility = publication.collection.visibility
        publication.save(update_fields=["visibility"])


def clear_visibility(apps, schema_editor):
    """Reversible: blank is what the field meant before this migration ran."""
    FortiPublication = apps.get_model("georiva_publisher_forti", "FortiPublication")
    FortiPublication.objects.update(visibility="")


class Migration(migrations.Migration):
    dependencies = [
        ("georiva_publisher_forti", "0001_initial"),
        ("georivacore", "0015_alter_variable_value_max_alter_variable_value_min"),
    ]

    operations = [
        migrations.RenameField(
            model_name="fortipublication",
            old_name="area",
            new_name="slug",
        ),
        migrations.AlterField(
            model_name="fortipublication",
            name="slug",
            field=models.SlugField(
                blank=True,
                help_text=(
                    "The model name a consumer asks for: GET /api/forecast/{slug}/. Unique "
                    "within the organisation, in the same grammar as the catalog slug it is "
                    "prefilled from — leave it blank to take that. Immutable once published: "
                    "it is a segment of every storage key."
                ),
            ),
        ),
        migrations.AddField(
            model_name="fortipublication",
            name="visibility",
            field=models.CharField(
                blank=True,
                choices=[("public", "Public"), ("private", "Private")],
                default="",
                help_text=(
                    "Who may ask for this model. Blank takes the collection's, which is the "
                    "usual answer; it may be narrowed from there but never widened past it. A "
                    "caller who may not see a model finds it absent from the listing and 404s "
                    "on it directly, so the endpoint cannot be used to enumerate what a tenant "
                    "publishes."
                ),
                max_length=10,
            ),
        ),
        migrations.RunPython(inherit_visibility_from_collection, clear_visibility),
        migrations.AlterField(
            model_name="fortipublication",
            name="collection",
            field=models.OneToOneField(
                help_text=(
                    "The forecast collection this model publishes. An internal collection is "
                    "refused: it is a derivation intermediate, not a dataset."
                ),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="forti_publication",
                to="georivacore.collection",
            ),
        ),
        migrations.AlterField(
            model_name="fortipublication",
            name="published_version",
            field=models.BigIntegerField(
                blank=True,
                editable=False,
                help_text="ref_epoch_seconds * 100 + revision — the integer latest/<area key> holds.",
                null=True,
            ),
        ),
    ]
