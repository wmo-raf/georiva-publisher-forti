"""Point every sink this plugin builds at a directory the test owns.

Two things have to move together, and missing either one files real objects into
the instance's real publications bucket — which is how a stray ``jsonformat.json``
turned up under ``test-org/forti/`` on the dev bucket once already.

``override_settings(STORAGES=...)`` alone is not enough. ``storage`` is a
singleton that caches a ``Bucket`` per type, and ``Bucket`` caches its Django
backend on first use, so a suite that touched the publications bucket before the
override keeps writing through the handle it already had.

So the bucket is built *after* the override, and :func:`~models.instance_sink` is
replaced with one that hands it out. Patching that one function is enough because
it is the only place a sink is constructed — ``FortiPublication.sink`` calls it,
and so does the config writer.
"""

import shutil
import tempfile

from django.conf import settings
from django.test import override_settings

from georiva.core.publishing import PublicationSink
from georiva.core.storage import Bucket, BucketType
from georiva_publisher_forti import models


class TemporarySinkMixin:
    """A ``TestCase`` mixin: call :meth:`isolate_sink` from ``setUp``."""

    def isolate_sink(self):
        self.bucket_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bucket_root, ignore_errors=True)

        override = override_settings(
            STORAGES={
                **settings.STORAGES,
                "georiva-publications": {
                    "BACKEND": "django.core.files.storage.FileSystemStorage",
                    "OPTIONS": {"location": self.bucket_root, "base_url": "/publications/"},
                },
            }
        )
        override.enable()
        self.addCleanup(override.disable)

        # After the override, so the handle resolves the redirected backend.
        self.bucket = Bucket(BucketType.PUBLICATIONS, "georiva-publications")

        bucket = self.bucket
        original = models.instance_sink
        models.instance_sink = lambda: PublicationSink.instance_wide(
            models.SINK_ROOT,
            marker_patterns=models.MARKER_PATTERNS,
            bucket=bucket,
        )
        self.addCleanup(setattr, models, "instance_sink", original)

        return self.bucket
