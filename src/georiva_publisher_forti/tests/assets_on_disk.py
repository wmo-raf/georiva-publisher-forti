"""Point core's assets bucket at a directory the test owns.

The publisher reads every value through core's read module (ADR 0046 §10),
which opens a local asset through ``storage.assets``. So the one seam between
this plugin's tests and object storage is that bucket's backend: it is
redirected to a filesystem directory, and the singleton's cached handle is
dropped so it resolves the redirected backend — then restored, so no later
test inherits it.
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import override_settings

from georiva.core.storage import storage


class AssetsOnDiskMixin:
    """A ``TestCase`` mixin: :meth:`isolate_assets` returns the directory an
    Asset's ``href`` is a path under."""

    def isolate_assets(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        override = override_settings(
            STORAGES={
                **settings.STORAGES,
                "georiva-assets": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": directory.name, "base_url": "/assets/"},
                },
            }
        )
        override.enable()
        self.addCleanup(override.disable)
        self.enterContext(patch.object(storage.assets, "_storage", None))
        return Path(directory.name)
