"""Focused owned-runtime cleanup regression tests. No Docker daemon or network."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STOP = ROOT / "install" / "stop-owned-runtime.sh"
CID = "a" * 64
NID = "b" * 64
TOKEN = "focused-cleanup-owner-token"
BOOT = Path("/proc/sys/kernel/random/boot_id")

FAKE_DOCKER = r"""
import json, os, sys
from pathlib import Path
db = Path(os.environ["DIFY_TEST_DOCKER_DB"])
doc = json.loads(db.read_text())
args = sys.argv[1:]
mode = doc.get("mode", "")
with db.with_suffix(".calls").open("a") as log:
    log.write(json.dumps(args) + "\n")
def save():
    db.write_text(json.dumps(doc))
def fail(message="daemon operation failed"):
    print(message, file=sys.stderr)
    raise SystemExit(1)
if args == ["info"]:
    if mode == "unavailable":
        fail()
elif args and args[0] == "ps":
    if mode == "ps_error":
        fail()
    print("\n".join(doc["containers"]))
elif args and args[0] == "inspect":
    kind = args[args.index("--type") + 1]
    ident = args[-1]
    if mode == "inspect_error":
        fail("permission denied")
    collection = doc["containers"] if kind == "container" else doc["networks"]
    if ident not in collection:
        fail("Error: No such object: " + ident)
    value = collection[ident]
    print(("true" if value else "false") if kind == "container" else str(value))
elif args and args[0] == "stop":
    if mode == "stop_error":
        fail()
    doc["containers"][args[-1]] = False
    save()
elif args and args[0] == "rm":
    if mode == "rm_error":
        fail()
    if mode != "rm_still_present":
        doc["containers"].pop(args[-1], None)
    save()
elif args[:2] == ["network", "rm"]:
    if mode == "network_rm_error":
        fail()
    doc["networks"].pop(args[-1], None)
    save()
else:
    fail("unexpected fake Docker command")
"""

@unittest.skipUnless(sys.platform == "linux" and BOOT.is_file(), "Linux process identities required")
class TestOwnedRuntimeCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dify-owned-cleanup-")
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        self.state.mkdir(mode=0o700)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        sudo = self.bin / "sudo"
        sudo.write_text("#!/bin/sh\nexit 1\n")
        sudo.chmod(0o755)
        self.db = self.base / "docker.json"
        self.env = os.environ.copy()
        self.env.pop("DIFY_TABLE_OWNER_TOKEN", None)
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env["PATH"]
        self.env["DIFY_TEST_DOCKER_DB"] = str(self.db)
        self.procs = []
        self._docker()
        self._owner()

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)
        self.tmp.cleanup()

    def _docker(self, mode="", containers=None, networks=None):
        self.db.write_text(json.dumps({
            "mode": mode,
            "containers": containers or {},
            "networks": networks or {},
        }))

    def _owner(self, proc=None, boot=None, pid=None, start=None, containers=None, networks=None):
        st = self.state.stat()
        doc = {
            "schema": "dify-table-csv.owned-runtime.v1",
            "owned_by": "dify-table-csv",
            "token": TOKEN,
            "state_dev": st.st_dev,
            "state_ino": st.st_ino,
            "project": "difytablecsv-abc12345",
            "reader_pid": pid if pid is not None else (proc.pid if proc else None),
            "reader_starttime": start if start is not None else (self._start(proc.pid) if proc else ""),
            "reader_boot_id": BOOT.read_text().strip() if boot is None else boot,
            "container_ids": containers or [],
            "network_ids": networks or [],
        }
        (self.state / "owner.json").write_text(json.dumps(doc))
        (self.state / "owner.token").write_text(TOKEN + "\n")
        (self.state / "marker").write_text("UNCHANGED\n")
        if proc:
            (self.state / "reader.pid").write_text(str(proc.pid) + "\n")

    @staticmethod
    def _start(pid):
        raw = Path("/proc/%s/stat" % pid).read_text()
        return raw[raw.rfind(")") + 2:].split()[19]

    def _sentinel(self, token=None):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            self.skipTest("safe Linux pidfd signalling is unavailable")
        script = self.base / "reader_boundary.mjs"
        script.write_text("import time\nprint('ready', flush=True)\ntime.sleep(300)\n")
        env = self.env.copy()
        if token:
            env["DIFY_TABLE_OWNER_TOKEN"] = token
        proc = subprocess.Popen(
            [sys.executable, str(script)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.procs.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def _stop(self):
        result = subprocess.run(
            ["bash", str(STOP), str(self.state)], env=self.env,
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual((self.state / "marker").read_text(), "UNCHANGED\n")
        return result

    def _assert_failure(self, result, marker):
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(marker, result.stdout)
        self.assertNotIn("stack_stopped=1", result.stdout)

    def test_same_boot_exact_reader_is_stopped(self):
        proc = self._sentinel()
        self._owner(proc)
        result = self._stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reader_stopped=1", result.stdout)
        self.assertIn("stack_stopped=1", result.stdout)
        proc.wait(timeout=5)

    def test_wrong_boot_with_matching_ticks_leaves_reader_alive(self):
        proc = self._sentinel()
        self._owner(proc, boot="different-boot")
        result = self._stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reader_pid_reused=1", result.stdout)
        self.assertNotIn("reader_stopped=1", result.stdout)
        self.assertIsNone(proc.poll())

    def test_wrong_recorded_pid_with_matching_ticks_leaves_reader_alive(self):
        proc = self._sentinel()
        self._owner(proc, pid=proc.pid + 1)
        result = self._stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reader_pid_reused=1", result.stdout)
        self.assertIsNone(proc.poll())

    def test_replacement_reader_with_owner_token_is_stopped(self):
        proc = self._sentinel(TOKEN)
        self._owner(proc, boot="old-boot", pid=proc.pid + 1, start="1")
        result = self._stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reader_stopped=1", result.stdout)
        proc.wait(timeout=5)

    def test_network_only_state_requires_reachable_docker(self):
        self._owner(networks=[NID])
        self._docker("unavailable", networks={NID: 0})
        self._assert_failure(self._stop(), "refuse: docker daemon is not reachable")

    def test_stop_remove_inspect_and_inventory_errors_are_not_success(self):
        for mode, marker in (
            ("stop_error", "container_stop_failed"),
            ("rm_error", "container_remove_failed"),
            ("inspect_error", "container_inspect_failed"),
            ("ps_error", "container_inventory_failed"),
            ("rm_still_present", "container_still_present"),
        ):
            with self.subTest(mode=mode):
                self._owner(containers=[CID])
                self._docker(mode, containers={CID: True})
                self._assert_failure(self._stop(), marker)

    def test_network_remove_and_inspect_errors_are_not_success(self):
        for mode, marker in (
            ("network_rm_error", "network_remove_failed"),
            ("inspect_error", "network_inspect_failed"),
        ):
            with self.subTest(mode=mode):
                self._owner(networks=[NID])
                self._docker(mode, networks={NID: 0})
                self._assert_failure(self._stop(), marker)

    def test_success_requires_absent_container_and_network_readback(self):
        self._owner(containers=[CID], networks=[NID])
        self._docker(containers={CID: True}, networks={NID: 0})
        result = self._stop()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("stack_stopped=1", result.stdout)
        remaining = json.loads(self.db.read_text())
        self.assertEqual(remaining["containers"], {})
        self.assertEqual(remaining["networks"], {})
        repeated = self._stop()
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertIn("stack_stopped=1", repeated.stdout)

if __name__ == "__main__":
    unittest.main()
