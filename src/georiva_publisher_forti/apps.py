from django.apps import AppConfig


class GeorivaPublisherFortiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "georiva_publisher_forti"
    verbose_name = "GeoRiva Forti Publisher"

    def ready(self):
        # Connecting here, in ready(), is what keeps the import direction ADR
        # 0020 protects: ingestion emits ``run_ingestion_closed`` knowing nothing
        # about who listens, and this plugin is the first subscriber.
        from . import signals  # noqa: F401
        from .forti_settings import check

        # A malformed GEORIVA_FORTI_* value stops the process here, naming the
        # variable, instead of at the first request that reads it.
        check()
