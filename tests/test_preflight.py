"""Preflight refuses before an image pull.

Positive, failure, and recovery cases use bounded fakes. A missing docker CLI
or a real interpreter without python3-venv is checked only when that is the
machine under test, and those runs still stop before docker pull.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_NAME = re.compile(r"^dify-table-state-[A-Za-z0-9]{6,}$")


def discard_state(stdout):
    match = re.search(r"^state_dir=(.+)$", stdout, re.M)
    if not match:
        return
    path = Path(match.group(1))
    if path.parent == Path("/tmp") and STATE_NAME.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.effects = self.work / "effects.log"
        self.bindir = self.work / "bin"
        self.bindir.mkdir()
        self._write_docker()
        self._write_python()
        for name in ("sudo", "git", "npm", "ip", "curl", "wget", "ssh"):
            self._write(name, "#!/bin/sh\nprintf '%s\\n' \"" + name + " $*\" >> \"" + str(self.effects) + "\"\nexit 1\n")
        self.pkg = self.work / "dify-table-csv"
        install = self.pkg / "install"
        install.mkdir(parents=True)
        for name in ("cold-install.sh", "state-boundary.py", "compose.pin.yml", "preflight.py"):
            source = ROOT / "install" / name
            target = install / name
            target.write_bytes(source.read_bytes())
            target.chmod(source.stat().st_mode)

    def _write(self, name, text):
        path = self.bindir / name
        path.write_text(text)
        path.chmod(0o755)

    def _write_docker(self):
        self._write(
            "docker",
            """#!/usr/bin/python3
import os, sys
log = os.environ["PREFLIGHT_EFFECTS"]
args = sys.argv[1:]
with open(log, "a", encoding="utf-8") as handle:
    handle.write("docker " + " ".join(args) + "\\n")
if args[:1] == ["info"]:
    raise SystemExit(0)
if args[:1] == ["ps"]:
    raise SystemExit(0)
if args[:2] == ["network", "inspect"]:
    raise SystemExit(1)
if args[:2] == ["compose", "version"]:
    if os.environ.get("PREFLIGHT_COMPOSE") == "missing":
        raise SystemExit(1)
    sys.stdout.write("Docker Compose version v2\\n")
    raise SystemExit(0)
if args[:1] == ["import"]:
    sys.stdin.read()
    raise SystemExit(0)
if args[:1] == ["create"]:
    if os.environ.get("PREFLIGHT_MOUNT") == "foreign":
        sys.stderr.write('invalid mount config for type "bind": bind source path does not exist\\n')
        raise SystemExit(1)
    if os.environ.get("PREFLIGHT_MOUNT") == "other":
        sys.stderr.write("mount probe refused by selected daemon\\n")
        raise SystemExit(1)
    raise SystemExit(0)
if "pull" in args or (args[:1] == ["compose"] and "up" in args):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("expensive\\n")
    raise SystemExit(99)
raise SystemExit(0)
""",
        )

    def _write_python(self):
        self._write(
            "python3",
            """#!/usr/bin/python3
import os, sys
real = "/usr/bin/python3"
if len(sys.argv) >= 3 and sys.argv[1] == "-m" and sys.argv[2] == "venv":
    with open(os.environ["PREFLIGHT_EFFECTS"], "a", encoding="utf-8") as handle:
        handle.write("python3 -m venv " + " ".join(sys.argv[3:]) + "\\n")
    dest = sys.argv[3]
    os.makedirs(dest, exist_ok=True)
    if os.environ.get("PREFLIGHT_VENV") == "missing":
        sys.stderr.write("The virtual environment was not created successfully because ensurepip is not available.\\n")
        sys.stderr.write("    apt install python3.12-venv\\n")
        raise SystemExit(1)
    os.makedirs(os.path.join(dest, "bin"), exist_ok=True)
    raise SystemExit(0)
os.execv(real, [real, *sys.argv[1:]])
""",
        )

    def env(self, **extra):
        env = os.environ.copy()
        for key in (
            "H64314_ADMIT_DIFY",
            "DIFY_TABLE_ADMIT",
            "DIFY_SOURCE",
            "H64314_DIFY_SRC",
            "DIFY_TABLE_PROJECT",
            "DIFY_TABLE_KEEP",
            "DIFY_TABLE_STATE",
            "PREFLIGHT_VENV",
            "PREFLIGHT_COMPOSE",
            "PREFLIGHT_MOUNT",
        ):
            env.pop(key, None)
        env["PATH"] = str(self.bindir) + ":/usr/bin:/bin"
        env["DIFY_TABLE_ADMIT"] = "1"
        env["PREFLIGHT_EFFECTS"] = str(self.effects)
        env.update(extra)
        return env

    def invoke(self, **extra):
        completed = subprocess.run(
            ["bash", str(self.pkg / "install" / "cold-install.sh")],
            cwd=self.pkg,
            env=self.env(**extra),
            text=True,
            capture_output=True,
            check=False,
        )
        self.addCleanup(lambda: discard_state(completed.stdout))
        return completed

    def effect_text(self):
        if not self.effects.is_file():
            return ""
        return self.effects.read_text(encoding="utf-8")

    def assert_no_expensive(self, text):
        self.assertNotIn("docker_pull_begin", text)
        self.assertNotIn("\ndocker pull", "\n" + self.effect_text())
        self.assertNotIn("compose up", self.effect_text())
        self.assertNotIn("expensive", self.effect_text())
        self.assertNotIn("sudo ", self.effect_text())

    def test_positive_preflight_reaches_no_pull(self):
        completed = self.invoke()
        combined = completed.stdout + completed.stderr
        self.assertIn("preflight_ok=1", completed.stdout, combined)
        self.assertIn("daemon_mount=visible", completed.stdout, combined)
        self.assert_no_expensive(combined)
        self.assertIn("git init", self.effect_text())
        self.assertNotEqual(completed.returncode, 0)

    def test_missing_venv_has_no_import_or_pull(self):
        completed = self.invoke(PREFLIGHT_VENV="missing")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("not_installed", completed.stdout)
        self.assertIn("refuse: python venv is not available", completed.stdout)
        self.assertIn("python3-venv", completed.stdout)
        self.assertNotIn("preflight_ok=1", completed.stdout)
        self.assert_no_expensive(combined)
        self.assertNotIn("docker import", self.effect_text())
        self.assertNotIn("git ", self.effect_text())
        self.assertNotIn("preflight-venv", completed.stdout)

    def test_missing_compose_has_no_import_or_pull(self):
        completed = self.invoke(PREFLIGHT_COMPOSE="missing")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: docker compose is not available for the selected docker", completed.stdout)
        self.assertIn("does not switch engines", completed.stdout)
        self.assert_no_expensive(combined)
        self.assertNotIn("docker import", self.effect_text())
        self.assertNotIn("git ", self.effect_text())

    def test_foreign_mount_has_no_pull(self):
        completed = self.invoke(PREFLIGHT_MOUNT="foreign")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker daemon cannot bind-mount this machine's files", completed.stdout)
        self.assertIn("bind source path does not exist", completed.stdout)
        self.assertIn("does not switch engines", completed.stdout)
        self.assert_no_expensive(combined)
        self.assertIn("docker import", self.effect_text())
        self.assertIn("docker create", self.effect_text())
        self.assertIn("--pull never", self.effect_text())
        self.assertNotIn("git ", self.effect_text())
        self.assertIn("docker image rm", self.effect_text())

    def test_other_mount_failure_has_no_pull(self):
        completed = self.invoke(PREFLIGHT_MOUNT="other")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker daemon mount probe failed", completed.stdout)
        self.assert_no_expensive(combined)
        self.assertNotIn("git ", self.effect_text())

    def test_recovery_after_missing_venv(self):
        failed = self.invoke(PREFLIGHT_VENV="missing")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.effects.write_text("", encoding="utf-8")
        recovered = self.invoke()
        combined = recovered.stdout + recovered.stderr
        self.assertIn("preflight_ok=1", recovered.stdout, combined)
        self.assert_no_expensive(combined)
        self.assertIn("docker import", self.effect_text())

    def test_real_missing_docker_stops_before_pull(self):
        if shutil.which("docker"):
            self.skipTest("a docker CLI is installed")
        env = os.environ.copy()
        env.pop("H64314_ADMIT_DIFY", None)
        env.pop("DIFY_SOURCE", None)
        env.pop("H64314_DIFY_SRC", None)
        env.pop("DIFY_TABLE_STATE", None)
        env["DIFY_TABLE_ADMIT"] = "1"
        completed = subprocess.run(
            ["bash", str(ROOT / "install" / "cold-install.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.addCleanup(lambda: discard_state(completed.stdout))
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: docker daemon is not reachable", completed.stdout)
        self.assertIn("not_installed", completed.stdout)
        self.assertNotIn("docker_pull_begin", combined)
        self.assertNotIn("preflight_ok=1", completed.stdout)

    def test_real_missing_venv_stops_before_pull_when_present(self):
        probe = Path(self.work) / "venv-capability"
        completed = subprocess.run(
            ["/usr/bin/python3", "-m", "venv", str(probe)],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.exists():
            shutil.rmtree(probe)
        if completed.returncode == 0:
            self.skipTest("this interpreter can create a venv")
        self._write(
            "python3",
            "#!/bin/sh\nexec /usr/bin/python3 \"$@\"\n",
        )
        run = self.invoke()
        combined = run.stdout + run.stderr
        self.assertEqual(run.returncode, 2, combined)
        self.assertIn("refuse: python venv is not available", run.stdout)
        self.assertIn("python3.12-venv", run.stdout)
        self.assert_no_expensive(combined)
        self.assertNotIn("docker import", self.effect_text())
        self.assertNotIn("git ", self.effect_text())
        match = re.search(r"^state_dir=(.+)$", run.stdout, re.M)
        self.assertIsNotNone(match)
        state = Path(match.group(1))
        self.assertFalse((state / "preflight-venv").exists())


if __name__ == "__main__":
    unittest.main()
