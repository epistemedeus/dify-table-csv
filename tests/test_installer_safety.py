"""Focused raw-source and normalized Compose regressions. No Docker or network."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class RawSourceTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL).decode().strip()

    def test_raw_export_ignores_replace_and_archive_attributes(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            src = parent / "src"
            src.mkdir()
            self.git(src, "init", "-q")
            self.git(src, "config", "user.name", "Test")
            self.git(src, "config", "user.email", "test@example.com")
            docker = src / "docker"
            docker.mkdir()
            config = docker / "docker-compose.yaml"
            config.write_text("services: {}\n")
            self.git(src, "add", "docker")
            self.git(src, "-c", "commit.gpgsign=false", "commit", "-qm", "original")
            original = self.git(src, "rev-parse", "HEAD")
            config.write_text("UNEXPECTED_REPLACEMENT\n")
            self.git(src, "add", "docker")
            self.git(src, "-c", "commit.gpgsign=false", "commit", "-qm", "replacement")
            replacement = self.git(src, "rev-parse", "HEAD")
            self.git(src, "replace", original, replacement)
            (src / ".git" / "info" / "attributes").write_text("docker/docker-compose.yaml export-ignore\n")
            outside = parent / "outside"
            outside.write_text("OUTSIDE_SECRET\n")
            (docker / "live-data").symlink_to(outside)
            (docker / ".env").write_text("LIVE_SECRET\n")
            result = subprocess.run(["bash", str(ROOT / "install/export-tracked-docker.sh"), str(src), str(parent / "export"), original], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            exported = parent / "export" / "docker"
            self.assertEqual((exported / "docker-compose.yaml").read_text(), "services: {}\n")
            self.assertFalse((exported / "live-data").exists())
            self.assertFalse((exported / ".env").exists())
            self.assertEqual(config.read_text(), "UNEXPECTED_REPLACEMENT\n")
            self.assertEqual(outside.read_text(), "OUTSIDE_SECRET\n")

    def test_raw_export_refuses_tracked_link_without_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder, "src")
            src.mkdir()
            self.git(src, "init", "-q")
            self.git(src, "config", "user.name", "Test")
            self.git(src, "config", "user.email", "test@example.com")
            (src / "docker").mkdir()
            (src / "docker" / "link").symlink_to("/outside")
            self.git(src, "add", "docker")
            self.git(src, "-c", "commit.gpgsign=false", "commit", "-qm", "link")
            commit = self.git(src, "rev-parse", "HEAD")
            dest = Path(folder, "export")
            result = subprocess.run(["bash", str(ROOT / "install/export-tracked-docker.sh"), str(src), str(dest), commit], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("refuse: tracked tree contains a symlink", result.stdout)
            self.assertFalse(dest.exists())


class ComposeHostMapTests(unittest.TestCase):
    def _check(self, hosts, expected=0, gateway="172.17.0.1", omit=None):
        document = {"services": {name: {"extra_hosts": hosts} for name in ("api", "worker", "ssrf_proxy")}}
        if omit:
            document["services"].pop(omit)
        result = subprocess.run([sys.executable, str(ROOT / "install" / "verify-compose-hosts.py"), gateway],
                                input=json.dumps(document), text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertIn("composed_host_docker_internal=3" if expected == 0 else "refuse:", result.stdout)

    def test_compose_normalized_host_map(self):
        self._check({"host.docker.internal": "172.17.0.1", "unrelated.example": "192.0.2.1"})

    def test_compose_list_equals(self):
        self._check(["host.docker.internal=172.17.0.1"])

    def test_compose_list_colon(self):
        self._check(["host.docker.internal:172.17.0.1"])

    def test_compose_missing_host_is_refused(self):
        self._check({"unrelated.example": "172.17.0.1"}, 2)

    def test_compose_missing_required_service_is_refused(self):
        self._check({"host.docker.internal": "172.17.0.1"}, 2, omit="worker")

    def test_compose_wrong_gateway_is_refused(self):
        self._check({"host.docker.internal": "172.17.0.2"}, 2)

    def test_compose_conflicting_duplicate_is_refused(self):
        self._check(["host.docker.internal:172.17.0.1", "host.docker.internal:172.17.0.2"], 2)

    def test_compose_invalid_gateway_is_refused(self):
        self._check({"host.docker.internal": "not-an-ip"}, 2, gateway="not-an-ip")


if __name__ == "__main__":
    unittest.main()
