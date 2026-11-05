"""Installer state root must stay outside the package source.

These cases run cold-install.sh from a package fixture under the test
TemporaryDirectory. External tools are bounded fakes. No Docker daemon and no
network. Cleanup deletes only that fixture and external state this process
recorded. Pre-existing source-adjacent directories, links, and runtime files
stay in place. The original-source control is the pinned 0.1.0 installer and
is expected to accept an in-source state directory.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Prefix siblings are planted under a temp root this process owns. The
# checkout parent is not writable on every host. The paths stay in ADJACENT,
# so the preservation checks still refuse to delete them.
SIBLING_ROOT = Path(tempfile.mkdtemp(prefix="h10200-sibling-root-"))
atexit.register(lambda: shutil.rmtree(SIBLING_ROOT, ignore_errors=True))


def neomorphic_checkout(package_root):
    """The Neomorphic tree when this package is examples/dify-table-csv inside it.

    A standalone extract has no such parent. Its grandparent is an ordinary
    directory, often /tmp, and must not become the cleanup boundary.
    """
    if len(package_root.parents) < 2:
        return None
    candidate = package_root.parents[1]
    marker = candidate / "examples" / "dify-table-csv"
    try:
        if marker.resolve() == package_root:
            return candidate
    except OSError:
        return None
    return None


CHECKOUT = neomorphic_checkout(ROOT)
BASE = "377fe1635c3fc0e527dbb04591776be825e9bcff"
ORIGINAL_SHA256 = "86bd580527864a1526d2a884ef1b3cc7e982b828046c46829af19d422ce9510b"
ORIGINAL_BYTES = 21154
INSTALLER = ROOT / "install" / "cold-install.sh"
HELPER = ROOT / "install" / "state-boundary.py"
PIN = ROOT / "install" / "compose.pin.yml"
FIXTURE = ROOT / "tests" / "fixtures" / "original-cold-install-0.1.0.sh"
MARKER_NAME = ".h10200-owned-sentinel"
SENTINEL_RUNTIME = b"SENTINEL_RUNTIME\n"
EXTERNAL_NAME = re.compile(r"^dify-table-state-[A-Za-z0-9]{6,}$")
CLEARED = (
    "H64314_WEAVIATE_DIGEST",
    "H64314_ADMIT_DIFY",
    "DIFY_TABLE_ADMIT",
    "DIFY_SOURCE",
    "H64314_DIFY_SRC",
    "DIFY_TABLE_PROJECT",
    "DIFY_TABLE_KEEP",
    "DIFY_TABLE_STATE",
    "DIFY_TABLE_OWNER_TOKEN",
    "READER_CORE",
)
FAKES = ("docker", "sudo", "git", "npm", "ip", "curl", "wget", "ssh")
SECRET_NAMES = ("local-admin.txt", "owner.json", "owner.token", "runtime.env", ".env")


def sibling_path(suffix):
    return SIBLING_ROOT / (ROOT.name + suffix)


ADJACENT = (
    ROOT / "local-state",
    ROOT / "install" / "nested-state",
    ROOT / "inside-link",
    sibling_path("-h10200-sibling"),
    sibling_path("-h10200-rel"),
    sibling_path("-h10200-via-link"),
)


def tree_signature(root):
    rows = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        current = Path(dirpath)
        listed = current.lstat()
        rows.append(("d", current.relative_to(root).as_posix(), stat.S_IMODE(listed.st_mode)))
        for name in sorted(filenames):
            path = current / name
            info = path.lstat()
            rel = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                rows.append(("l", rel, mode, os.readlink(path)))
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append(("f", rel, mode, info.st_size, digest))
            else:
                rows.append(("o", rel, mode, info.st_size))
    return tuple(rows)


def adjacent_record(path):
    if path.is_symlink():
        info = path.lstat()
        return ("link", stat.S_IMODE(info.st_mode), os.readlink(path))
    if not path.exists():
        return ("absent",)
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        children = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            child_info = child.lstat()
            kind = "link" if stat.S_ISLNK(child_info.st_mode) else "dir" if stat.S_ISDIR(child_info.st_mode) else "file"
            children.append((child.name, kind, stat.S_IMODE(child_info.st_mode)))
        return ("dir", mode, tuple(children))
    if stat.S_ISREG(info.st_mode):
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if info.st_size <= 1_000_000 else None
        return ("file", mode, info.st_size, digest)
    return ("other", mode)


class InstallerStateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.real_tree = tree_signature(ROOT)
        self.real_adjacent = tuple(adjacent_record(path) for path in ADJACENT)
        self.owned_external = []
        self.owned_sentinels = []
        self.sentinel_token = hashlib.sha256(os.urandom(16)).hexdigest()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self._assert_real_source_untouched)
        self.addCleanup(self._discard_owned_external)
        self.addCleanup(self._release_all_sentinels)
        self.work = Path(self.tmp.name)
        self.effects = self.work / "effects.log"
        self.bindir = self.work / "bin"
        self.bindir.mkdir()
        for name in FAKES:
            path = self.bindir / name
            path.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"" + name + " $*\" >> \"" + str(self.effects) + "\"\n"
                "exit 1\n"
            )
            path.chmod(0o755)
        self.prior = self.work / "prior-runtime"
        self.prior.mkdir(mode=0o750)
        self.prior_marker = self.prior / "db"
        self.prior_marker.write_bytes(b"SYNTHETIC_PRIOR_RUNTIME\n")
        os.chmod(self.prior_marker, 0o600)
        os.chmod(self.prior, 0o750)
        self.unrelated = self.work / "unrelated.bin"
        self.unrelated.write_bytes(b"UNRELATED_BYTES\n")
        self.prior_mode = stat.S_IMODE(self.prior.stat().st_mode)
        self.marker_mode = stat.S_IMODE(self.prior_marker.stat().st_mode)
        self.marker_bytes = self.prior_marker.read_bytes()
        self.unrelated_bytes = self.unrelated.read_bytes()
        self.pkg = self._make_package()
        env = os.environ.copy()
        for key in CLEARED:
            env.pop(key, None)
        env["PATH"] = str(self.bindir) + ":/usr/bin:/bin"
        env["DIFY_TABLE_ADMIT"] = "1"
        self.env = env

    def _make_package(self):
        package = self.work / "dify-table-csv"
        install = package / "install"
        install.mkdir(parents=True)
        script = install / "cold-install.sh"
        script.write_bytes(INSTALLER.read_bytes())
        script.chmod(0o755)
        helper = install / "state-boundary.py"
        helper.write_bytes(HELPER.read_bytes())
        helper.chmod(0o644)
        pin = install / "compose.pin.yml"
        pin.write_bytes(PIN.read_bytes())
        pin.chmod(0o644)
        return package

    def _assert_real_source_untouched(self):
        self.assertEqual(tuple(adjacent_record(path) for path in ADJACENT), self.real_adjacent)
        self.assert_same_tree(self.real_tree)

    def _source_adjacent(self, path):
        path = Path(path)
        if path in ADJACENT or path == ROOT or (CHECKOUT is not None and path == CHECKOUT):
            return True
        if ROOT in path.parents:
            return True
        if CHECKOUT is not None and CHECKOUT in path.parents:
            return True
        return False

    def _own_external(self, path):
        path = Path(path)
        if self._source_adjacent(path):
            raise AssertionError("refusing to own a source-adjacent path %s" % path)
        if path.parent != Path("/tmp") or EXTERNAL_NAME.fullmatch(path.name) is None:
            raise AssertionError("refusing to own an external path %s" % path)
        if path.is_symlink() or not path.is_dir():
            raise AssertionError("refusing to own a path that is not a directory %s" % path)
        if any(path.iterdir()):
            raise AssertionError("refusing to own a non-empty external directory %s" % path)
        if path not in self.owned_external:
            self.owned_external.append(path)

    def _delete_owned_external(self, path):
        path = Path(path)
        if self._source_adjacent(path):
            raise AssertionError("refusing to delete a source-adjacent path %s" % path)
        if path not in self.owned_external:
            raise AssertionError("refusing to delete an unrecorded path %s" % path)
        if path.parent != Path("/tmp") or EXTERNAL_NAME.fullmatch(path.name) is None:
            raise AssertionError("refusing to delete an external path %s" % path)
        if path.is_symlink() or not path.is_dir():
            raise AssertionError("refusing to delete a path that is not a directory %s" % path)
        if any(path.iterdir()):
            raise AssertionError("refusing to delete a non-empty external directory %s" % path)
        shutil.rmtree(path)

    def _discard_owned_external(self):
        for path in list(self.owned_external):
            if not path.exists() and not path.is_symlink():
                continue
            self._delete_owned_external(path)

    def _plant_sentinel(self, path, kind):
        if path.exists() or path.is_symlink():
            raise AssertionError("refusing to plant over an existing path %s" % path)
        if self._source_adjacent(path) is False:
            raise AssertionError("sentinel is not source-adjacent %s" % path)
        if kind == "dir":
            path.mkdir()
            runtime = path / "runtime.marker"
            runtime.write_bytes(SENTINEL_RUNTIME)
            os.chmod(runtime, 0o600)
            (path / MARKER_NAME).write_text(self.sentinel_token + "\n")
        elif kind == "link":
            target = self.work / "sentinel-link-target"
            target.mkdir(exist_ok=True)
            path.symlink_to(target)
        else:
            raise AssertionError("unknown sentinel kind %s" % kind)
        self.owned_sentinels.append((path, kind))

    def _release_one(self, path, kind):
        if kind == "link":
            expected = str(self.work / "sentinel-link-target")
            if not path.is_symlink() or os.readlink(path) != expected:
                raise AssertionError("refusing to unlink an unowned path %s" % path)
            path.unlink()
            return
        marker = path / MARKER_NAME
        if path.is_symlink() or not path.is_dir():
            raise AssertionError("refusing to delete an unowned path %s" % path)
        if not marker.is_file() or marker.read_text() != self.sentinel_token + "\n":
            raise AssertionError("refusing to delete an unowned path %s" % path)
        shutil.rmtree(path)

    def _release_all_sentinels(self):
        pending = list(self.owned_sentinels)
        self.owned_sentinels = []
        for index, (path, kind) in enumerate(pending):
            if not path.exists() and not path.is_symlink():
                continue
            try:
                self._release_one(path, kind)
            except Exception:
                self.owned_sentinels = pending[index:]
                raise

    def _note_stdout_state(self, stdout):
        match = re.search(r"^state_dir=(/tmp/dify-table-state-\S+)$", stdout, re.M)
        if match is None:
            return
        self._own_external(Path(match.group(1)))

    def run_install(self, state, cwd=None, extra=None, script=None, admit=True):
        env = dict(self.env)
        if not admit:
            env.pop("DIFY_TABLE_ADMIT", None)
            env.pop("H64314_ADMIT_DIFY", None)
        if state is None:
            env.pop("DIFY_TABLE_STATE", None)
        else:
            env["DIFY_TABLE_STATE"] = state
        if extra:
            env.update(extra)
        if self.effects.exists():
            self.effects.unlink()
        completed = subprocess.run(
            ["bash", str(script or (self.pkg / "install" / "cold-install.sh"))],
            cwd=str(cwd or self.pkg),
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        self._note_stdout_state(completed.stdout)
        return completed

    def effect_names(self):
        if not self.effects.exists():
            return []
        return [line.split(" ", 1)[0] for line in self.effects.read_text().splitlines()]

    def assert_preserved(self):
        self.assertEqual(self.prior_marker.read_bytes(), self.marker_bytes)
        self.assertEqual(self.unrelated.read_bytes(), self.unrelated_bytes)
        self.assertEqual(stat.S_IMODE(self.prior_marker.stat().st_mode), self.marker_mode)
        self.assertEqual(stat.S_IMODE(self.prior.stat().st_mode), self.prior_mode)

    def assert_same_tree(self, before):
        after = tree_signature(ROOT)
        if before == after:
            return
        removed = [row for row in before if row not in after][:12]
        added = [row for row in after if row not in before][:12]
        self.fail("package source changed\nremoved=%s\nadded=%s" % (removed, added))

    def assert_no_secrets(self, path):
        if not path.exists():
            return
        for name in SECRET_NAMES:
            self.assertFalse((path / name).exists(), name)

    def assert_inside_refusal(self, completed):
        blob = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, blob)
        self.assertIn("refuse: state directory is inside the package source", completed.stdout)
        self.assertIn("not_installed", completed.stdout)
        self.assertIn("does not docker pull", completed.stdout)
        self.assertNotIn("state_dir=", completed.stdout)
        self.assertNotIn("owned_project=", completed.stdout)
        self.assertNotIn("docker_pull_begin", completed.stdout)
        self.assertNotIn("local_admin_written", completed.stdout)
        self.assertEqual(self.effect_names(), [])
        self.assert_preserved()

    def assert_outside_accepted(self, completed, raw, created):
        blob = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, blob)
        self.assertIn("state_dir=" + raw, completed.stdout)
        self.assertIn("refuse: docker daemon is not reachable", completed.stdout)
        self.assertNotIn("refuse: state directory is inside the package source", completed.stdout)
        self.assertNotIn("docker_pull_begin", completed.stdout)
        self.assertNotIn("local_admin_written", completed.stdout)
        names = self.effect_names()
        self.assertGreaterEqual(len(names), 1, blob)
        self.assertEqual(names[0], "docker")
        self.assertNotIn("git", names)
        self.assertNotIn("npm", names)
        self.assertNotIn("curl", names)
        self.assertNotIn("wget", names)
        self.assertTrue(created.is_dir(), created)
        self.assertFalse(created.is_symlink())
        self.assertEqual(stat.S_IMODE(created.stat().st_mode), 0o700)
        self.assert_no_secrets(created)
        self.assert_preserved()

    def test_check_is_ordered_before_state_creation(self):
        text = INSTALLER.read_text()
        check = text.index('python3 "$ROOT/install/state-boundary.py"')
        create = text.index('mkdir "$state"')
        default = text.index("mktemp -d /tmp/dify-table-state-")
        docker = text.index("docker info")
        self.assertLess(check, create)
        self.assertLess(check, default)
        self.assertLess(create, docker)
        self.assertLess(default, docker)
        self.assertIn("refuse: state directory already exists", text)
        self.assertIn("collision_refused", text)
        self.assertIn("refuse: state parent is not a directory", text)

    def test_absolute_and_relative_children_refuse(self):
        before = self.real_tree
        absolute = str(self.pkg / "local-state")
        refused = self.run_install(absolute)
        self.assert_inside_refusal(refused)
        self.assertFalse((self.pkg / "local-state").exists())
        nested = str(self.pkg / "install" / "nested-state")
        nested_run = self.run_install(nested)
        self.assert_inside_refusal(nested_run)
        self.assertFalse((self.pkg / "install" / "nested-state").exists())
        relative = self.run_install("local-state", cwd=self.pkg)
        self.assert_inside_refusal(relative)
        dotted = self.run_install("./local-state", cwd=self.pkg)
        self.assert_inside_refusal(dotted)
        caller = self.work / "caller"
        caller.mkdir()
        outside = self.run_install("local-state", cwd=caller)
        self.assert_outside_accepted(outside, "local-state", caller / "local-state")
        self.assertFalse((self.pkg / "local-state").exists())
        self.assert_same_tree(before)

    def test_equal_root_refuses_without_changing_source(self):
        before = self.real_tree
        root_mode = stat.S_IMODE(self.pkg.stat().st_mode)
        for candidate, cwd in (
            (str(self.pkg), self.work),
            (str(self.pkg) + "/", self.work),
            (".", self.pkg),
            ("./.", self.pkg),
            (str(self.pkg) + "/install/..", self.work),
        ):
            completed = self.run_install(candidate, cwd=cwd)
            self.assert_inside_refusal(completed)
            self.assertEqual(stat.S_IMODE(self.pkg.stat().st_mode), root_mode)
        self.assert_same_tree(before)

    def test_trailing_and_parent_components_refuse(self):
        before = self.real_tree
        samples = (
            str(self.pkg) + "/local-state/",
            str(self.pkg) + "/local-state/.",
            str(self.pkg) + "/./local-state",
            str(self.pkg) + "//local-state",
            str(self.pkg) + "/install/../local-state",
            "install/../local-state",
        )
        for candidate in samples:
            cwd = self.pkg if not candidate.startswith("/") else self.work
            completed = self.run_install(candidate, cwd=cwd)
            self.assert_inside_refusal(completed)
            self.assertFalse((self.pkg / "local-state").exists())
        self.assert_same_tree(before)

    def test_symlink_ancestor_and_source_alias_refuse(self):
        before = self.real_tree
        alias = self.work / "alias-pkg"
        alias.symlink_to(self.pkg)
        through_alias = self.run_install(str(alias / "local-state"))
        self.assert_inside_refusal(through_alias)
        self.assertFalse((self.pkg / "local-state").exists())
        via_script = self.run_install(
            str(alias / "local-state"),
            script=alias / "install" / "cold-install.sh",
        )
        self.assert_inside_refusal(via_script)
        physical = self.run_install(str(self.pkg / "local-state"), script=alias / "install" / "cold-install.sh")
        self.assert_inside_refusal(physical)
        relative_alias = self.run_install("local-state", cwd=alias, script=alias / "install" / "cold-install.sh")
        self.assert_inside_refusal(relative_alias)
        self.assertFalse((self.pkg / "local-state").exists())
        link_install = self.work / "link-install"
        link_install.symlink_to(self.pkg / "install")
        crossed = self.run_install(str(link_install) + "/../local-state")
        self.assert_inside_refusal(crossed)
        self.assertFalse((self.pkg / "local-state").exists())
        self.assertFalse((self.work / "local-state").exists())
        into_source = self.work / "into-source"
        into_source.symlink_to(self.pkg)
        child = self.run_install(str(into_source / "local-state"))
        self.assert_inside_refusal(child)
        self.assertFalse((self.pkg / "local-state").exists())
        self.assert_same_tree(before)

    def test_prefix_sibling_and_link_escape_are_outside(self):
        before = self.real_tree
        sibling = self.pkg.parent / (self.pkg.name + "-h10200-sibling")
        self.assertTrue(str(sibling).startswith(str(self.pkg)))
        self.assertFalse(str(sibling).startswith(str(self.pkg) + os.sep))
        self.assertTrue(self.work in sibling.parents)
        raw = str(sibling)
        completed = self.run_install(raw)
        self.assert_outside_accepted(completed, raw, sibling)
        self.assert_same_tree(before)
        parent = self.pkg.parent / (self.pkg.name + "-h10200-rel")
        parent.mkdir()
        relative = "../" + parent.name + "/state"
        relative_run = self.run_install(relative, cwd=self.pkg)
        self.assert_outside_accepted(relative_run, relative, parent / "state")
        via = self.pkg.parent / (self.pkg.name + "-h10200-via-link")
        via.mkdir()
        link_install = self.work / "link-install"
        link_install.symlink_to(self.pkg / "install")
        escaped = str(link_install) + "/../../" + via.name + "/state"
        escaped_run = self.run_install(escaped)
        self.assert_outside_accepted(escaped_run, escaped, via / "state")
        self.assertFalse((self.pkg / "local-state").exists())
        self.assert_same_tree(before)

    def test_ordinary_outside_target_keeps_the_supplied_path(self):
        before = self.real_tree
        parent = self.work / "outside-parent"
        parent.mkdir()
        raw = str(parent / "state")
        completed = self.run_install(raw)
        self.assert_outside_accepted(completed, raw, parent / "state")
        spelled = str(parent) + "/./kept-path"
        spelled_run = self.run_install(spelled)
        self.assert_outside_accepted(spelled_run, spelled, parent / "kept-path")
        self.assertFalse((self.pkg / "local-state").exists())
        self.assert_same_tree(before)

    def test_legacy_admission_flag_accepts_outside_state(self):
        before = self.real_tree
        parent = self.work / "legacy-parent"
        parent.mkdir()
        raw = str(parent / "state")
        completed = self.run_install(raw, extra={"DIFY_TABLE_ADMIT": "", "H64314_ADMIT_DIFY": "1"})
        self.assertNotIn("refuse: admission flag is unset", completed.stdout)
        self.assertIn("H64314_ADMIT_DIFY=1", completed.stdout)
        self.assert_outside_accepted(completed, raw, parent / "state")
        self.assert_same_tree(before)

    def test_existing_foreign_targets_and_parent_symlink_stay_closed(self):
        before = self.real_tree
        foreign = self.work / "foreign"
        foreign.mkdir()
        marker = foreign / "marker"
        marker.write_bytes(b"UNCHANGED_STATE_MARKER\n")
        os.chmod(foreign, 0o755)
        mode = stat.S_IMODE(foreign.stat().st_mode)
        completed = self.run_install(str(foreign))
        blob = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, blob)
        self.assertIn("refuse: state directory already exists", completed.stdout)
        self.assertNotIn("inside the package source", completed.stdout)
        self.assertEqual(marker.read_bytes(), b"UNCHANGED_STATE_MARKER\n")
        self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), mode)
        self.assertEqual(self.effect_names(), [])
        trailed = self.run_install(str(foreign) + "/")
        self.assertIn("refuse: state directory already exists", trailed.stdout)
        self.assertEqual(marker.read_bytes(), b"UNCHANGED_STATE_MARKER\n")
        link = self.work / "foreign-link"
        link.symlink_to(foreign)
        linked = self.run_install(str(link))
        self.assertIn("refuse: state directory already exists", linked.stdout)
        self.assertEqual(marker.read_bytes(), b"UNCHANGED_STATE_MARKER\n")
        self.assertEqual(self.effect_names(), [])
        parent_link = self.work / "parent-link"
        parent_link.symlink_to(foreign)
        child = self.run_install(str(parent_link / "new-child"))
        self.assertEqual(child.returncode, 2, child.stdout + child.stderr)
        self.assertIn("refuse: state parent is not a directory", child.stdout)
        self.assertFalse((foreign / "new-child").exists())
        self.assertEqual(marker.read_bytes(), b"UNCHANGED_STATE_MARKER\n")
        self.assertEqual(self.effect_names(), [])
        inside = self.pkg / "install"
        inside_mode = stat.S_IMODE(inside.stat().st_mode)
        pin = (inside / "compose.pin.yml").read_bytes()
        existing_inside = self.run_install(str(inside))
        self.assert_inside_refusal(existing_inside)
        self.assertEqual(stat.S_IMODE(inside.stat().st_mode), inside_mode)
        self.assertEqual((inside / "compose.pin.yml").read_bytes(), pin)
        self.assert_same_tree(before)
        self.assert_preserved()

    def test_safe_default_lands_outside_the_source(self):
        before = self.real_tree
        completed = self.run_install(None)
        match = re.search(r"^state_dir=(/tmp/dify-table-state-\S+)$", completed.stdout, re.M)
        self.assertIsNotNone(match, completed.stdout + completed.stderr)
        created = Path(match.group(1))
        self.assertIn(created, self.owned_external)
        self.assert_outside_accepted(completed, match.group(1), created)
        self.assertNotEqual(created, self.pkg)
        self.assertFalse(str(created).startswith(str(self.pkg) + os.sep))
        self.assertFalse(str(created).startswith(str(ROOT) + os.sep))
        self.assert_same_tree(before)

    def test_restoration_after_refusal(self):
        before = self.real_tree
        refused = self.run_install(str(self.pkg / "local-state"))
        self.assert_inside_refusal(refused)
        self.assertFalse((self.pkg / "local-state").exists())
        parent = self.work / "recovered"
        parent.mkdir()
        raw = str(parent / "state")
        accepted = self.run_install(raw)
        self.assert_outside_accepted(accepted, raw, parent / "state")
        self.assertFalse((self.pkg / "local-state").exists())
        self.assert_no_secrets(self.pkg)
        self.assert_same_tree(before)

    def test_no_admission_behavior_is_unchanged(self):
        before = self.real_tree
        inside = self.run_install(str(self.pkg / "local-state"), admit=False)
        blob = inside.stdout + inside.stderr
        self.assertEqual(inside.returncode, 2, blob)
        self.assertIn("installed_admission_boundary", inside.stdout)
        self.assertIn("refuse: admission flag is unset", inside.stdout)
        self.assertIn("not_installed", inside.stdout)
        self.assertIn("does not docker pull", inside.stdout)
        self.assertNotIn("refuse: state directory is inside the package source", inside.stdout)
        self.assertNotIn("state_dir=", inside.stdout)
        self.assertFalse((self.pkg / "local-state").exists())
        self.assertEqual(self.effect_names(), [])
        outside = self.run_install(str(self.work / "noadmit-state"), admit=False)
        self.assertIn("refuse: admission flag is unset", outside.stdout)
        self.assertFalse((self.work / "noadmit-state").exists())
        self.assertEqual(self.effect_names(), [])
        self.assert_same_tree(before)
        self.assert_preserved()

    def test_invalid_project_still_refuses_before_state_creation(self):
        before = self.real_tree
        completed = self.run_install(str(self.pkg / "local-state"), extra={"DIFY_TABLE_PROJECT": "difytable"})
        blob = completed.stdout + completed.stderr
        self.assertEqual(completed.returncode, 2, blob)
        self.assertIn("refuse: project name", completed.stdout)
        self.assertNotIn("inside the package source", completed.stdout)
        self.assertNotIn("state_dir=", completed.stdout)
        self.assertFalse((self.pkg / "local-state").exists())
        self.assertEqual(self.effect_names(), [])
        self.assert_same_tree(before)

    def test_cleanup_preserves_preexisting_source_adjacent_state(self):
        planted = []
        specs = tuple((path, "link" if path.name == "inside-link" else "dir") for path in ADJACENT)
        self.assertEqual(SIBLING_ROOT, sibling_path("-h10200-sibling").parent)
        self.assertNotEqual(sibling_path("-h10200-sibling").parent, Path("/"))
        self.assertTrue(os.access(SIBLING_ROOT, os.W_OK))
        for path, kind in specs:
            if path.exists() or path.is_symlink():
                continue
            self._plant_sentinel(path, kind)
            planted.append(path)
        self.assertGreaterEqual(len(planted), 1)
        refused = self.run_install(str(self.pkg / "local-state"))
        self.assert_inside_refusal(refused)
        for path in ADJACENT:
            with self.assertRaises(AssertionError):
                self._delete_owned_external(path)
        for path, kind in self.owned_sentinels:
            self.assertTrue(path.exists() or path.is_symlink(), path)
            if kind == "dir":
                self.assertEqual((path / "runtime.marker").read_bytes(), SENTINEL_RUNTIME)
                self.assertEqual(stat.S_IMODE((path / "runtime.marker").stat().st_mode), 0o600)
        stray = Path("/tmp") / "dify-table-state-notowned"
        stray.mkdir()
        try:
            (stray / MARKER_NAME).write_text(self.sentinel_token + "\n")
            with self.assertRaises(AssertionError):
                self._delete_owned_external(stray)
            self.assertTrue(stray.is_dir())
        finally:
            if stray.is_dir() and (stray / MARKER_NAME).is_file() and (stray / MARKER_NAME).read_text() == self.sentinel_token + "\n":
                shutil.rmtree(stray)
        self._release_all_sentinels()
        self.assert_same_tree(self.real_tree)

    def test_original_source_control_creates_inside_state(self):
        blob = FIXTURE.read_bytes()
        self.assertEqual(len(blob), ORIGINAL_BYTES)
        self.assertEqual(hashlib.sha256(blob).hexdigest(), ORIGINAL_SHA256)
        self.assertNotIn(b"state-boundary.py", blob)
        if CHECKOUT is not None:
            probe = subprocess.run(
                ["git", "-C", str(CHECKOUT), "cat-file", "-e", BASE + ":examples/dify-table-csv/install/cold-install.sh"],
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                git_blob = subprocess.check_output(
                    ["git", "-C", str(CHECKOUT), "show", BASE + ":examples/dify-table-csv/install/cold-install.sh"]
                )
                self.assertEqual(git_blob, blob)
        package = self.work / "original-pkg"
        script = package / "install" / "cold-install.sh"
        script.parent.mkdir(parents=True)
        script.write_bytes(blob)
        script.chmod(0o755)
        state = package / "local-state"
        original = self.run_install(str(state), cwd=package, script=script)
        detail = original.stdout + original.stderr
        self.assertEqual(original.returncode, 2, detail)
        self.assertTrue(state.is_dir(), detail)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertIn("state_dir=" + str(state), original.stdout)
        self.assertNotIn("refuse: state directory is inside the package source", original.stdout)
        self.assertEqual(self.effect_names()[0], "docker")
        self.assert_no_secrets(state)
        fixed = self.run_install(str(self.pkg / "local-state"))
        self.assert_inside_refusal(fixed)
        self.assertFalse((self.pkg / "local-state").exists())
        outside = package.parent / "original-outside"
        outside.mkdir()
        original_outside = self.run_install(str(outside / "state"), cwd=package, script=script)
        self.assert_outside_accepted(original_outside, str(outside / "state"), outside / "state")


if __name__ == "__main__":
    unittest.main()
