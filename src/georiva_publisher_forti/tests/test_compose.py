"""The deployment artifact, and the four ways it can silently stop matching.

``deploy/compose.yml`` is static — there is no renderer to unit-test any more —
but it holds four facts that also live in Python, and every one of them fails
quietly when the two drift:

- the **names of the config documents**. The sidecar fetches them by basename
  and each service is pointed at one by path. Rename either in :mod:`config` and
  the sidecar goes on fetching a key that is no longer written, the healthcheck
  never passes, and the pair simply never starts.
- the **prefix**. A ``FORTI_PREFIX`` that disagrees with ``models.SINK_ROOT``
  produces an empty config directory and no error anywhere.
- the **compose version**, which is the whole point of M5.8's drift check. A
  version that is not bumped with the plugin's reports agreement it has not
  checked, which is worse than reporting nothing.
- the **escaping**. Compose interpolates ``${...}`` inside inline config
  content, so a shell expansion written with one ``$`` is resolved at ``up``
  time — usually to the empty string — and the script that reaches the
  container is not the script in this repository.

The rest is the trap the two directions share: the status document
``rawdataforecaster.json`` and the config document ``rawdataforecaster.json``
have the same basename, so a pass that got a direction wrong would overwrite one
with the other and the next ``watcher.Load()`` would be fatal. Those tests are
about what the script *cannot* do, not about what it does.
"""

import re
from importlib.metadata import version
from pathlib import Path

import yaml
from django.test import SimpleTestCase

from georiva_publisher_forti import config, models

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "deploy" / "compose.yml"

SYNC_SCRIPT_CONFIG = "forti-sync-script"
SIDECAR = "forti-config-sync"
FORECASTER = "forti-rawdataforecaster"
FRONTEND = "forti-jsonfrontend"


def load():
    return yaml.safe_load(COMPOSE_PATH.read_text())


def sync_script(document=None):
    """The script as it is written here — *before* compose interpolates it."""
    document = document or load()
    return document["configs"][SYNC_SCRIPT_CONFIG]["content"]


def sync_code(document=None):
    """The script with its commentary removed.

    The comments name the things the script must not do — ``mc mirror``, a
    bucket listing — so a test for their absence has to read the code and not
    the prose that explains why they are absent.
    """
    return "\n".join(line for line in sync_script(document).splitlines() if not line.lstrip().startswith("#"))


def flag(service, name):
    """The value following ``name`` in a service's command list."""
    command = service["command"]
    return command[command.index(name) + 1]


class CompatibilityTests(SimpleTestCase):
    """Facts the compose file shares with Python, asserted from both ends."""

    def setUp(self):
        self.document = load()
        self.services = self.document["services"]
        self.script = sync_script(self.document)

    def test_the_sidecar_fetches_exactly_the_documents_config_writes(self):
        """``config.refresh`` writes two paths; the sidecar fetches two names.

        Asserted as equality rather than membership: a third document written
        and not fetched never reaches the pair, and a third fetched and not
        written keeps ``fetch_ok`` false forever.
        """
        fetched = set(re.findall(r"fetch_config (\S+)", self.script))
        written = {Path(path).name for path in (config.JSONFORMAT_PATH, config.RAWDATAFORECASTER_PATH)}

        self.assertEqual(fetched, written)

    def test_the_sidecar_looks_in_the_directory_config_writes_to(self):
        self.assertIn(f'/$$FORTI_PREFIX/{config.CONFIG_PREFIX}"', self.script)

    def test_the_prefix_is_the_instance_wide_sink_root(self):
        self.assertEqual(self.services[SIDECAR]["environment"]["FORTI_PREFIX"], models.SINK_ROOT)

    def test_each_service_reads_the_document_the_sidecar_fetches_for_it(self):
        """The other half of the first test: fetched into the volume, and read
        back out of it under the same name."""
        self.assertEqual(
            flag(self.services[FORECASTER], "-config"),
            f"/config/{Path(config.RAWDATAFORECASTER_PATH).name}",
        )
        self.assertEqual(
            flag(self.services[FRONTEND], "-config"),
            f"/config/{Path(config.JSONFORMAT_PATH).name}",
        )

    def test_the_healthcheck_waits_for_both_documents(self):
        """``Load()`` is fatal on a missing config file in both processes, so
        the pair waits on the first successful sync instead of crash-looping."""
        test = " ".join(self.services[SIDECAR]["healthcheck"]["test"])

        for path in (config.RAWDATAFORECASTER_PATH, config.JSONFORMAT_PATH):
            self.assertIn(f"/config/{Path(path).name}", test)

    def test_the_compose_version_is_the_plugin_version(self):
        """The premise of M5.8's drift check, checked here so it cannot become
        a comparison of one constant against itself."""
        self.assertEqual(
            self.services[SIDECAR]["environment"]["FORTI_COMPOSE_VERSION"],
            version("georiva-publisher-forti"),
        )


class DirectionTests(SimpleTestCase):
    """``status/rawdataforecaster.json`` and ``config/rawdataforecaster.json``
    share a basename. These are the reasons a typo cannot swap them."""

    def setUp(self):
        self.code = sync_code()

    def test_nothing_mirrors_between_the_two_trees(self):
        self.assertNotIn("mc mirror", self.code)

    def test_the_two_directions_have_separate_local_directories(self):
        self.assertIn("CONFIG_DIR=/config", self.code)
        self.assertIn("STATUS_DIR=/status", self.code)

    def test_a_status_document_is_refused_at_a_config_path(self):
        """Keyed on ``loaded_sha`` and not on ``areas``.

        The rawdataforecaster *status* file reports its resident areas under
        ``"areas"`` too (`forecast.go:409`, M5.1), so the obvious token does not
        separate the two documents. ``loaded_sha`` is a ``configwatch.Status``
        field with no ``omitempty``: in every status document, in neither
        config document.
        """
        self.assertIn('"loaded_sha"', self.code)

    def test_the_config_side_never_lists_the_bucket(self):
        """It fetches two names it already knows. A listing is how a document
        nobody wrote ends up somewhere it is read."""
        self.assertNotIn("mc ls", self.code)

    def test_the_upload_destination_is_never_computed(self):
        """Every upload names ``STATUS_REMOTE`` literally, so however the local
        glob turns out this direction cannot reach ``config/``."""
        destinations = set(re.findall(r"mc cp [^\n]*?\"(\$\$[A-Z_]+)/\"", self.code))

        self.assertEqual(destinations, {"$$STATUS_REMOTE"})


class ArtifactTests(SimpleTestCase):
    def setUp(self):
        self.document = load()
        self.services = self.document["services"]

    def test_neither_service_publishes_a_port(self):
        """D11/D19: the plugin's own view is the only way in, because the
        ``auth_request`` gate cannot front these (`addresses.py:129`)."""
        for name, service in self.services.items():
            self.assertNotIn("ports", service, f"{name} publishes a port")

    def test_credentials_are_referenced_never_embedded(self):
        """This file is committed. It carries variable references and compose
        resolves them from core's ``.env`` at up time."""
        rendered = COMPOSE_PATH.read_text()

        self.assertIn("AWS_ACCESS_KEY_ID: ${MINIO_ROOT_USER}", rendered)
        self.assertIn("${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD}}", rendered)

    def test_the_sync_script_is_run_by_a_named_interpreter(self):
        """A ``configs:`` entry with inline content lands mode 0444. Nothing
        chmods it, so the exec bit must not be relied on."""
        self.assertEqual(self.services[SIDECAR]["entrypoint"][0], "/bin/sh")
        self.assertEqual(self.services[SIDECAR]["entrypoint"][1], "/opt/forti/sync.sh")

    def test_every_shell_expansion_in_the_script_is_escaped(self):
        """Compose interpolates inline config content. A bare ``$`` here is
        substituted at ``up`` time and never reaches the container."""
        remainder = sync_script(self.document).replace("$$", "")

        self.assertNotIn("$", remainder)

    def test_the_images_are_pinned_and_not_the_spike(self):
        """M5.1 settled on ``v0.8.1-wmo.1`` with the fork's git sha as a label.
        ``:spike`` is a name that has to be unlearned."""
        images = [service["image"] for service in self.services.values()]

        self.assertNotIn(":spike", " ".join(images))
        for image in images:
            self.assertRegex(image, r":[^}]+}$", f"{image} floats")
