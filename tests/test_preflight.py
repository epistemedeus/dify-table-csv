"""Preflight refuses before an image pull.

The docker fake enforces Moby's create rule: an image with no command and a
create with no command exits "no command specified". Positive, failure,
recovery, collision, and hang cases use that fake. A real isolated daemon is
used only when its socket is already running.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_NAME = re.compile(r"^dify-table-state-[A-Za-z0-9]{6,}$")
PROBE_SOCK = Path(os.environ.get("DIFY_TABLE_PROBE_SOCK", "/tmp/h22300-probe/docker.sock"))
PROBE_DOCKER = Path(os.environ.get("DIFY_TABLE_DOCKER_BIN", "/tmp/h22300-probe/docker/docker"))
PROBE_HOME = Path(os.environ.get("DIFY_TABLE_PROBE_HOME", "/tmp/h22300-probe/home"))


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
            r'''#!/usr/bin/python3
import json, os, sys, time
log = os.environ["PREFLIGHT_EFFECTS"]
state_path = log + ".json"
args = sys.argv[1:]

def remember(text):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(text + "\n")
        handle.flush()

remember("docker " + " ".join(args))

def load():
    if not os.path.exists(state_path):
        return {}
    with open(state_path, encoding="utf-8") as handle:
        return json.load(handle)

def save(state):
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)

def positionals(argv, valued):
    found = []
    index = 0
    flags = {}
    while index < len(argv):
        item = argv[index]
        if item in valued:
            flags.setdefault(item, []).append(argv[index + 1])
            index += 2
            continue
        if item == "--privileged":
            flags["--privileged"] = ["1"]
            index += 1
            continue
        if item.startswith("-"):
            index += 1
            continue
        found.append(item)
        index += 1
    return found, flags

if args[:1] == ["info"]:
    raise SystemExit(0)
if args[:1] == ["ps"]:
    raise SystemExit(0)
if args[:2] == ["network", "inspect"]:
    raise SystemExit(1)
if args[:2] == ["compose", "version"]:
    if os.environ.get("PREFLIGHT_COMPOSE") == "missing":
        raise SystemExit(1)
    sys.stdout.write("Docker Compose version v2\n")
    raise SystemExit(0)
if args[:1] == ["import"]:
    if os.environ.get("PREFLIGHT_HANG") == "import":
        time.sleep(120)
    sys.stdin.read()
    found, flags = positionals(args[1:], {"--change", "-c", "--message", "-m", "--platform"})
    changes = flags.get("--change", []) + flags.get("-c", [])
    has_command = any(item.strip().startswith("CMD") or item.strip().startswith("ENTRYPOINT") for item in changes)
    tags = [item for item in found if item != "-"]
    if tags:
        remember("import-tag " + tags[0])
    image_id = "sha256:" + ("12" * 32)
    save({"image_id": image_id, "image_has_command": has_command, "container_id": None})
    sys.stdout.write(image_id + "\n")
    raise SystemExit(0)
if args[:2] == ["image", "inspect"]:
    image_id = args[-1]
    state = load()
    if image_id != state.get("image_id"):
        sys.stderr.write("No such image\n")
        raise SystemExit(1)
    if os.environ.get("PREFLIGHT_TAGS") == "present":
        sys.stdout.write("[\"foreign:latest\"]\n")
    else:
        sys.stdout.write("null\n")
    raise SystemExit(0)
if args[:1] == ["create"]:
    if os.environ.get("PREFLIGHT_HANG") == "create":
        time.sleep(120)
    found, flags = positionals(args[1:], {"--pull", "--network", "--mount", "--name", "--label", "--entrypoint"})
    if "--privileged" in flags or any(item not in ("none",) for item in flags.get("--network", [])):
        sys.stderr.write("network or privileged probe refused\n")
        raise SystemExit(1)
    state = load()
    image = found[0] if found else ""
    command = found[1:]
    image_ready = state.get("image_has_command") and image == state.get("image_id")
    if not command and not image_ready:
        sys.stderr.write("no command specified\n")
        raise SystemExit(1)
    if os.environ.get("PREFLIGHT_MOUNT") == "foreign":
        sys.stderr.write("invalid mount config for type \"bind\": bind source path does not exist\n")
        raise SystemExit(1)
    if os.environ.get("PREFLIGHT_MOUNT") == "other":
        sys.stderr.write("mount probe refused by selected daemon\n")
        raise SystemExit(1)
    if os.environ.get("PREFLIGHT_CREATE") == "conflict":
        sys.stderr.write("Conflict. The container name \"/foreign-sentinel\" is already in use by container " + ("ff" * 32) + "\n")
        raise SystemExit(1)
    container_id = "34" * 32
    state["container_id"] = container_id
    save(state)
    sys.stdout.write(container_id + "\n")
    raise SystemExit(0)
if args[:1] == ["inspect"]:
    container_id = args[-1]
    state = load()
    if container_id != state.get("container_id"):
        sys.stderr.write("No such container\n")
        raise SystemExit(1)
    image_id = state.get("image_id")
    if os.environ.get("PREFLIGHT_INSPECT") == "mismatch":
        image_id = "sha256:" + ("ee" * 32)
    sys.stdout.write(container_id + " " + image_id + " false\n")
    raise SystemExit(0)
if args[:1] == ["rm"]:
    targets = [item for item in args[1:] if item != "-f"]
    state = load()
    for target in targets:
        if target != state.get("container_id"):
            remember("foreign-rm " + target)
            continue
        if os.environ.get("PREFLIGHT_RM") == "fail":
            sys.stderr.write("container rm failed\n")
            raise SystemExit(1)
        state["container_id"] = None
        save(state)
    raise SystemExit(0)
if args[:2] == ["image", "rm"] or args[:1] == ["rmi"]:
    image_id = args[-1]
    state = load()
    if image_id != state.get("image_id"):
        remember("foreign-image-rm " + image_id)
        raise SystemExit(0)
    if os.environ.get("PREFLIGHT_RMI") == "fail":
        sys.stderr.write("image rm failed\n")
        raise SystemExit(1)
    state["image_id"] = None
    save(state)
    raise SystemExit(0)
if "pull" in args or (args[:1] == ["compose"] and "up" in args):
    remember("expensive")
    raise SystemExit(99)
raise SystemExit(0)
''',
        )

    def _write_python(self):
        self._write(
            "python3",
            r'''#!/usr/bin/python3
import os, sys, time
real = "/usr/bin/python3"
if len(sys.argv) >= 3 and sys.argv[1] == "-m" and sys.argv[2] == "venv":
    with open(os.environ["PREFLIGHT_EFFECTS"], "a", encoding="utf-8") as handle:
        handle.write("python3 -m venv " + " ".join(sys.argv[3:]) + "\n")
    dest = sys.argv[3]
    os.makedirs(dest, exist_ok=True)
    if os.environ.get("PREFLIGHT_VENV") == "hang":
        time.sleep(120)
    if os.environ.get("PREFLIGHT_VENV") == "missing":
        sys.stderr.write("The virtual environment was not created successfully because ensurepip is not available.\n")
        sys.stderr.write("    apt install python3.12-venv\n")
        raise SystemExit(1)
    os.makedirs(os.path.join(dest, "bin"), exist_ok=True)
    raise SystemExit(0)
os.execv(real, [real, *sys.argv[1:]])
''',
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
            "PREFLIGHT_CREATE",
            "PREFLIGHT_HANG",
            "PREFLIGHT_RM",
            "PREFLIGHT_RMI",
            "PREFLIGHT_INSPECT",
            "PREFLIGHT_TAGS",
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

    def assert_no_foreign_delete(self):
        self.assertNotIn("foreign-rm ", self.effect_text())
        self.assertNotIn("foreign-image-rm ", self.effect_text())
        self.assertNotIn("import-tag ", self.effect_text())
        self.assertNotIn("docker rm -f", self.effect_text())

    def test_positive_preflight_reaches_no_pull(self):
        completed = self.invoke()
        combined = completed.stdout + completed.stderr
        effects = self.effect_text()
        self.assertIn("preflight_ok=1", completed.stdout, combined)
        self.assertIn("daemon_mount=visible", completed.stdout, combined)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
        self.assertRegex(effects, r'docker import --change CMD \["true"\] -')
        self.assertRegex(
            effects,
            r"docker create --pull never --network none --mount type=bind,source=\S+,target=/probe,readonly sha256:[0-9a-f]{64} true",
        )
        self.assertRegex(effects, r"docker inspect --format \{\{\.Id\}\} \{\{\.Image\}\} \{\{\.State\.Running\}\} [0-9a-f]{64}")
        self.assertRegex(effects, r"docker rm [0-9a-f]{64}")
        self.assertRegex(effects, r"docker image rm sha256:[0-9a-f]{64}")
        self.assertNotIn("--privileged", effects)
        self.assertNotIn("--name", effects)
        self.assertIn("git init", effects)
        self.assertNotEqual(completed.returncode, 0)

    def test_moby_contract_rejects_create_without_command(self):
        env = self.env()
        docker = str(self.bindir / "docker")
        imported = subprocess.run([docker, "import", "-"], input=b"not-a-command\n", capture_output=True, env=env, check=False)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        image = imported.stdout.decode().strip()
        self.assertRegex(image, r"^sha256:[0-9a-f]{64}$")
        created = subprocess.run(
            [docker, "create", "--pull", "never", "--network", "none", "--mount", "type=bind,source=/tmp,target=/probe,readonly", image],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertNotEqual(created.returncode, 0, created.stdout + created.stderr)
        self.assertIn("no command specified", created.stderr)
        self.assertNotIn("docker rm", self.effect_text())
        accepted = subprocess.run(
            [docker, "create", "--pull", "never", "--network", "none", "--mount", "type=bind,source=/tmp,target=/probe,readonly", image, "true"],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertRegex(accepted.stdout.strip(), r"^[0-9a-f]{64}$")

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
        effects = self.effect_text()
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker daemon cannot bind-mount this machine's files", completed.stdout)
        self.assertIn("bind source path does not exist", completed.stdout)
        self.assertIn("does not switch engines", completed.stdout)
        self.assertNotIn("no command specified", combined)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
        self.assertIn("docker import", effects)
        self.assertIn(" true", effects)
        self.assertIn("--pull never", effects)
        self.assertNotIn("\ndocker rm ", "\n" + effects)
        self.assertIn("docker image rm sha256:", effects)
        self.assertNotIn("git ", effects)

    def test_other_mount_failure_has_no_pull(self):
        completed = self.invoke(PREFLIGHT_MOUNT="other")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker daemon mount probe failed", completed.stdout)
        self.assertIn("mount probe refused by selected daemon", completed.stdout)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
        self.assertNotIn("\ndocker rm ", "\n" + self.effect_text())
        self.assertNotIn("git ", self.effect_text())

    def test_failed_create_does_not_remove_unreceived_container(self):
        completed = self.invoke(PREFLIGHT_CREATE="conflict")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("foreign-sentinel", completed.stdout)
        self.assertIn("refuse: selected docker daemon mount probe failed", completed.stdout)
        self.assertNotIn("preflight_ok=1", completed.stdout)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
        self.assertNotIn("\ndocker rm ", "\n" + self.effect_text())
        self.assertIn("docker image rm sha256:", self.effect_text())

    def test_unverified_container_is_not_removed(self):
        completed = self.invoke(PREFLIGHT_INSPECT="mismatch")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("could not verify container ownership", completed.stdout)
        self.assertNotIn("daemon_mount=visible", completed.stdout)
        self.assert_no_foreign_delete()
        self.assertNotIn("\ndocker rm ", "\n" + self.effect_text())
        self.assertIn("docker image rm sha256:", self.effect_text())

    def test_cleanup_failure_is_not_success(self):
        completed = self.invoke(PREFLIGHT_RM="fail")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker mount probe cleanup failed", completed.stdout)
        self.assertIn("cleanup of container", completed.stdout)
        self.assertNotIn("daemon_mount=visible", completed.stdout)
        self.assertNotIn("preflight_ok=1", completed.stdout)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()

    def test_cleanup_failure_preserves_mount_refusal(self):
        completed = self.invoke(PREFLIGHT_MOUNT="foreign", PREFLIGHT_RMI="fail")
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertIn("refuse: selected docker daemon cannot bind-mount this machine's files", completed.stdout)
        self.assertIn("bind source path does not exist", completed.stdout)
        self.assertIn("cleanup of image", completed.stdout)
        self.assertNotIn("preflight_ok=1", completed.stdout)
        self.assertNotIn("daemon_mount=visible", completed.stdout)
        self.assert_no_foreign_delete()

    def test_hanging_docker_is_bounded(self):
        started = time.monotonic()
        completed = self.invoke(PREFLIGHT_HANG="create")
        elapsed = time.monotonic() - started
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertLess(elapsed, 30, combined)
        self.assertGreaterEqual(elapsed, 15, combined)
        self.assertIn("refuse: selected docker command timed out", completed.stdout)
        self.assertIn("docker create timed out", completed.stdout)
        self.assertNotIn("preflight_ok=1", completed.stdout)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
        self.assertNotIn("\ndocker rm ", "\n" + self.effect_text())
        self.assertIn("docker image rm sha256:", self.effect_text())

    def test_hanging_venv_is_bounded(self):
        started = time.monotonic()
        completed = self.invoke(PREFLIGHT_VENV="hang")
        elapsed = time.monotonic() - started
        combined = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, combined)
        self.assertLess(elapsed, 30, combined)
        self.assertGreaterEqual(elapsed, 15, combined)
        self.assertIn("refuse: python venv check timed out", completed.stdout)
        self.assertNotIn("docker import", self.effect_text())
        self.assert_no_expensive(combined)
        match = re.search(r"^state_dir=(.+)$", completed.stdout, re.M)
        self.assertIsNotNone(match)
        self.assertFalse((Path(match.group(1)) / "preflight-venv").exists())

    def test_recovery_after_missing_venv(self):
        failed = self.invoke(PREFLIGHT_VENV="missing")
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.effects.write_text("", encoding="utf-8")
        recovered = self.invoke()
        combined = recovered.stdout + recovered.stderr
        self.assertIn("preflight_ok=1", recovered.stdout, combined)
        self.assert_no_expensive(combined)
        self.assert_no_foreign_delete()
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

    def test_real_isolated_daemon_mount_cleanup_and_red_create(self):
        if not PROBE_SOCK.is_socket() or not PROBE_DOCKER.is_file():
            self.skipTest("isolated probe daemon is not running")
        env = os.environ.copy()
        env["HOME"] = str(PROBE_HOME)
        env["PATH"] = str(PROBE_DOCKER.parent) + ":" + env.get("PATH", "")
        env.pop("DOCKER_HOST", None)
        docker = [str(PROBE_DOCKER), "-H", "unix://" + str(PROBE_SOCK)]

        def run(args, input_bytes=None):
            return subprocess.run(
                [*docker, *args],
                input=input_bytes,
                capture_output=True,
                env=env,
                check=False,
            )

        def ids(kind):
            args = ["ps", "-aq"] if kind == "ps" else ["images", "-aq"]
            completed = run(args)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return [line for line in completed.stdout.decode().splitlines() if line]

        def same(left, right):
            left = left.removeprefix("sha256:")
            right = right.removeprefix("sha256:")
            return left.startswith(right) or right.startswith(left)

        self.assertEqual(ids("ps"), [])
        bare = subprocess.run(
            [sys.executable, "-c", "import pathlib,sys; sys.path.insert(0, sys.argv[1]); import preflight; sys.stdout.buffer.write(preflight.rootfs_tar())", str(ROOT / "install")],
            capture_output=True, check=False,
        )
        self.assertEqual(bare.returncode, 0, bare.stderr)
        red_import = run(["import", "-"], input_bytes=bare.stdout)
        self.assertEqual(red_import.returncode, 0, red_import.stderr)
        red_image = red_import.stdout.decode().strip()
        red_create = run(["create", "--pull", "never", "--network", "none", red_image])
        self.assertNotEqual(red_create.returncode, 0, red_create.stdout)
        self.assertIn(b"no command specified", red_create.stderr)
        self.assertEqual(ids("ps"), [])
        removed = run(["image", "rm", red_image])
        self.assertEqual(removed.returncode, 0, removed.stderr)

        foreign_import = run(["import", "--change", 'CMD ["true"]', "-"], input_bytes=bare.stdout)
        self.assertEqual(foreign_import.returncode, 0, foreign_import.stderr)
        foreign_image = foreign_import.stdout.decode().strip()
        foreign = run(["create", "--pull", "never", "--network", "none", "--name", "foreign-sentinel", foreign_image, "true"])
        self.assertEqual(foreign.returncode, 0, foreign.stderr)
        foreign_id = foreign.stdout.decode().strip()

        def remove_foreign_fixture():
            # addCleanup is LIFO. Removing the image while this container
            # still holds it returns nonzero and leaves the image behind.
            removed = run(["rm", foreign_id])
            self.assertEqual(removed.returncode, 0, removed.stderr)
            image_removed = run(["image", "rm", foreign_image])
            self.assertEqual(image_removed.returncode, 0, image_removed.stderr)
            self.assertEqual(ids("ps"), [])
            self.assertEqual(ids("images"), [])

        self.addCleanup(remove_foreign_fixture)

        missing = run([
            "create", "--pull", "never", "--network", "none",
            "--mount", "type=bind,source=/tmp/h22300-probe-missing-path,target=/probe,readonly",
            foreign_image, "true",
        ])
        self.assertNotEqual(missing.returncode, 0, missing.stdout)
        self.assertIn(b"bind source path does not exist", missing.stderr)
        self.assertEqual(len(ids("ps")), 1)
        self.assertTrue(any(same(foreign_id, item) for item in ids("ps")))
        before_images = set(ids("images"))

        state = Path(self.work) / "real-state"
        state.mkdir()
        import importlib.util
        spec = importlib.util.spec_from_file_location("dify_table_preflight", ROOT / "install" / "preflight.py")
        preflight = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preflight)
        previous_home = os.environ.get("HOME")
        os.environ["HOME"] = str(PROBE_HOME)
        try:
            from io import StringIO
            import contextlib
            output = StringIO()
            with contextlib.redirect_stdout(output):
                preflight.check_compose(docker)
                preflight.check_mount(str(state), docker)
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
        self.assertIn("daemon_mount=visible", output.getvalue())
        self.assertNotIn("refuse:", output.getvalue())
        still = run(["inspect", "--format", "{{.Id}} {{.Name}} {{.State.Running}}", foreign_id])
        self.assertEqual(still.returncode, 0, still.stderr)
        self.assertIn(b"foreign-sentinel", still.stdout)
        self.assertIn(b"false", still.stdout)
        self.assertEqual(len(ids("ps")), 1)
        self.assertTrue(any(same(foreign_id, item) for item in ids("ps")))
        self.assertEqual(set(ids("images")), before_images)
        self.assertTrue(any(same(foreign_image, item) for item in before_images))
        self.assertFalse((state / "mount-probe-token").exists())

        output = StringIO()
        os.environ["HOME"] = str(PROBE_HOME)
        try:
            with contextlib.redirect_stdout(output):
                preflight.check_mount(str(state), docker)
        finally:
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home
        self.assertIn("daemon_mount=visible", output.getvalue())
        self.assertEqual(len(ids("ps")), 1)
        self.assertTrue(any(same(foreign_id, item) for item in ids("ps")))
        self.assertEqual(set(ids("images")), before_images)


class _Completed:
    def __init__(self, code, out):
        self.returncode = code
        self.stdout = out
        self.stderr = b""


def load_forward_preflight():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dify_table_preflight_forward",
        ROOT / "install" / "preflight.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ForwardPolicyTests(unittest.TestCase):
    def assert_list_only(self, argv):
        banned = {"-P", "-A", "-I", "-D", "-F", "-X", "-Z", "--policy"}
        self.assertTrue(banned.isdisjoint(argv), argv)
        self.assertIn("-S", argv)
        self.assertIn("FORWARD", argv)

    def test_drop_listing_fails_fast_without_mutation(self):
        import contextlib
        import io
        preflight = load_forward_preflight()
        calls = []

        def run(argv):
            calls.append(list(argv))
            self.assert_list_only(argv)
            return "ok", _Completed(0, b"-P FORWARD DROP\n-A FORWARD -j DOCKER-USER\n")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                preflight.check_forward_policy(
                    run,
                    which=lambda name: "/usr/sbin/iptables-legacy" if name == "iptables-legacy" else None,
                )
        self.assertEqual(caught.exception.code, 2)
        text = output.getvalue()
        self.assertIn("forward_policy=drop", text)
        self.assertIn(
            "refuse: iptables-legacy FORWARD policy is DROP, so containers on a user-defined bridge cannot reach each other",
            text,
        )
        self.assertIn("Set the host filter FORWARD policy to ACCEPT and re-run. This installer does not change firewall policy.", text)
        self.assertEqual(calls, [["/usr/sbin/iptables-legacy", "-S", "FORWARD"]])

    def test_accept_listing_ignores_a_later_drop_rule(self):
        import contextlib
        import io
        preflight = load_forward_preflight()
        calls = []

        def run(argv):
            calls.append(list(argv))
            self.assert_list_only(argv)
            return "ok", _Completed(0, b"-P FORWARD ACCEPT\n-A FORWARD -j DROP\n")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            preflight.check_forward_policy(
                run,
                which=lambda name: "/usr/sbin/iptables-legacy" if name == "iptables-legacy" else None,
            )
        self.assertIn("forward_policy=accept", output.getvalue())
        self.assertNotIn("refuse:", output.getvalue())
        self.assertEqual(calls, [["/usr/sbin/iptables-legacy", "-S", "FORWARD"]])

    def test_chain_header_drop_is_detected(self):
        preflight = load_forward_preflight()
        self.assertTrue(preflight.forward_policy_is_drop("Chain FORWARD (policy DROP)\n"))
        self.assertFalse(preflight.forward_policy_is_drop("Chain FORWARD (policy ACCEPT)\n"))

    def test_missing_command_is_unread_and_does_not_run(self):
        import contextlib
        import io
        preflight = load_forward_preflight()

        def run(argv):
            raise AssertionError(argv)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            preflight.check_forward_policy(run, which=lambda name: None)
        self.assertEqual(output.getvalue().strip(), "forward_policy=unread")

    def test_permission_denied_uses_one_sudo_list(self):
        import contextlib
        import io
        preflight = load_forward_preflight()
        calls = []

        def run(argv):
            calls.append(list(argv))
            self.assert_list_only(argv)
            if argv[0] == "sudo":
                return "ok", _Completed(0, b"-P FORWARD DROP\n")
            return "ok", _Completed(4, b"")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                preflight.check_forward_policy(
                    run,
                    which=lambda name: "/usr/sbin/iptables-legacy" if name == "iptables-legacy" else None,
                )
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(
            calls,
            [
                ["/usr/sbin/iptables-legacy", "-S", "FORWARD"],
                ["sudo", "-n", "/usr/sbin/iptables-legacy", "-S", "FORWARD"],
            ],
        )
        self.assertIn("forward_policy=drop", output.getvalue())

    def test_sources_do_not_set_firewall_policy(self):
        preflight_text = (ROOT / "install" / "preflight.py").read_text(encoding="utf-8")
        installer = (ROOT / "install" / "cold-install.sh").read_text(encoding="utf-8")
        self.assertNotIn("iptables-legacy -P", preflight_text)
        self.assertNotIn("-P FORWARD ACCEPT", preflight_text)
        self.assertNotIn("iptables", installer)


if __name__ == "__main__":
    unittest.main()
