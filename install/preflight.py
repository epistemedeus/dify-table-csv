#!/usr/bin/env python3
"""Receive Python, venv, Compose, and the selected daemon's bind-mount view.

This runs after the installer has selected one docker CLI and before any
upstream fetch or image pull. It does not choose a different engine, require
a historical socket, or change host firewall rules.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def refuse(message, route):
    print("refuse: " + message)
    print("supported: " + route)
    raise SystemExit(2)


def remove_tree(path):
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def check_venv(state):
    python = shutil.which("python3")
    if not python:
        refuse(
            "python3 is not on PATH",
            "install python3 and re-run this script",
        )
    probe = Path(state) / "preflight-venv"
    if probe.exists() or probe.is_symlink():
        refuse(
            "python venv probe path already exists",
            "use a new state directory and re-run this script",
        )
    completed = subprocess.run(
        [python, "-m", "venv", str(probe)],
        capture_output=True,
        text=True,
    )
    detail = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
    try:
        if completed.returncode != 0:
            if "No module named venv" in detail or "ensurepip" in detail or "python3-venv" in detail:
                print("detail: python3-venv is not installed for this interpreter")
            elif detail:
                print("detail: " + detail.splitlines()[-1][:300])
            refuse(
                "python venv is not available",
                "install python3-venv for this interpreter and re-run. On Debian/Ubuntu the package is python3-venv or python3.12-venv.",
            )
    finally:
        remove_tree(probe)


def check_compose(docker):
    completed = subprocess.run(
        [*docker, "compose", "version"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        refuse(
            "docker compose is not available for the selected docker",
            "install the Compose plugin for this docker CLI and re-run. This script does not switch engines.",
        )


def rootfs_tar():
    payload = b"preflight\n"
    handle = io.BytesIO()
    with tarfile.open(fileobj=handle, mode="w") as archive:
        info = tarfile.TarInfo(name="preflight-empty")
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
    return handle.getvalue()


def check_mount(state, docker):
    token = Path(state) / "mount-probe-token"
    token.write_text("dify-table-mount-probe\n", encoding="utf-8")
    token.chmod(0o600)
    suffix = os.urandom(4).hex()
    tag = "dify-table-mount-probe:p" + suffix
    name = "dify-table-mount-probe-" + suffix
    imported = False
    try:
        imported_run = subprocess.run(
            [*docker, "import", "-", tag],
            input=rootfs_tar(),
            capture_output=True,
        )
        if imported_run.returncode != 0:
            detail = (imported_run.stderr or imported_run.stdout or b"").decode("utf-8", "replace").strip()
            if detail:
                print("detail: " + detail.splitlines()[-1][:300])
            refuse(
                "selected docker could not import the local mount-probe image",
                "use the selected docker CLI with a local image import and re-run. This script does not pull an image and does not switch engines.",
            )
        imported = True
        mount = "type=bind,source=%s,target=/probe,readonly" % token
        created = subprocess.run(
            [*docker, "create", "--pull", "never", "--name", name, "--mount", mount, tag],
            capture_output=True,
            text=True,
        )
        combined = (created.stderr or "") + "\n" + (created.stdout or "")
        if created.returncode != 0:
            if "bind source path does not exist" in combined:
                print("detail: bind source path does not exist")
                refuse(
                    "selected docker daemon cannot bind-mount this machine's files",
                    "select a daemon that bind-mounts this machine's filesystem and re-run. This script does not switch engines and does not require a historical socket path.",
                )
            tail = combined.strip().splitlines()
            if tail:
                print("detail: " + tail[-1][:300])
            refuse(
                "selected docker daemon mount probe failed",
                "use a daemon that accepts a readonly bind mount of this state directory and re-run. This script does not switch engines.",
            )
        print("daemon_mount=visible")
    finally:
        token.unlink(missing_ok=True)
        subprocess.run([*docker, "rm", "-f", name], capture_output=True)
        if imported:
            subprocess.run([*docker, "image", "rm", tag], capture_output=True)


def main(argv):
    if len(argv) < 3:
        refuse(
            "preflight arguments",
            "re-run install/cold-install.sh so it passes the state directory and the selected docker",
        )
    state = argv[1]
    docker = argv[2:]
    if not state.startswith("/") or not Path(state).is_dir() or Path(state).is_symlink():
        refuse(
            "preflight state directory is not a directory",
            "re-run install/cold-install.sh so it creates the state directory",
        )
    if not docker or any(item == "" for item in docker):
        refuse(
            "selected docker command is empty",
            "re-run install/cold-install.sh after docker info succeeds",
        )
    check_venv(state)
    check_compose(docker)
    check_mount(state, docker)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
