#!/usr/bin/env python3
"""Receive Python, venv, Compose, and the selected daemon's bind-mount view.

This runs after the installer has selected one docker CLI and before any
upstream fetch or image pull. It does not choose a different engine, require
a historical socket, or change host firewall rules.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


# A hung selected CLI must fail inside this bound. The value is long enough
# for a local import and create, and short enough that the installer returns.
PROBE_TIMEOUT_SECONDS = 15
INERT_COMMAND = "true"


def refuse(message, route):
    print("refuse: " + message)
    print("supported: " + route)
    raise SystemExit(2)


def remove_tree(path):
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def run_bounded(argv, input_bytes=None):
    try:
        completed = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout", None
    return "ok", completed


def decode_text(blob):
    return (blob or b"").decode("utf-8", "replace")


def last_line(text):
    lines = text.strip().splitlines()
    if not lines:
        return ""
    return lines[-1][:300]


def received_id(stdout, kind):
    text = decode_text(stdout).strip()
    if kind == "image":
        if re.fullmatch(r"sha256:[0-9a-f]{64}", text):
            return text
        return None
    match = re.fullmatch(r"(?:sha256:)?([0-9a-f]{64})", text)
    if not match:
        return None
    return match.group(1)


def same_id(left, right):
    def norm(value):
        value = value.strip().lower()
        if value.startswith("sha256:"):
            value = value[7:]
        return value
    left_norm = norm(left)
    return left_norm == norm(right) and re.fullmatch(r"[0-9a-f]{64}", left_norm) is not None


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
    status, completed = run_bounded([python, "-m", "venv", str(probe)])
    try:
        if status == "timeout":
            print("detail: python3 -m venv timed out")
            refuse(
                "python venv check timed out",
                "install python3-venv for this interpreter and re-run. On Debian/Ubuntu the package is python3-venv or python3.12-venv.",
            )
        detail = (decode_text(completed.stderr) + "\n" + decode_text(completed.stdout)).strip()
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
    status, completed = run_bounded([*docker, "compose", "version"])
    if status == "timeout":
        print("detail: docker compose version timed out")
        refuse(
            "docker compose check timed out",
            "install the Compose plugin for this docker CLI and re-run. This script does not switch engines.",
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
    """Import an untagged image and create a container that is not started.

    Moby mergeAndVerifyConfig rejects an empty Entrypoint and Cmd with
    "no command specified". The import sets CMD and create passes the same
    inert command. Nothing is started, and no registry pull is requested.
    Cleanup removes only an image id import returned and a container id
    create returned after inspect matches that image and the container is
    stopped. A failed or unknown create does not authorize a delete.
    """
    token = Path(state) / "mount-probe-token"
    token.write_text("dify-table-mount-probe\n", encoding="utf-8")
    token.chmod(0o600)
    image_id = None
    container_id = None
    image_owned = False
    container_owned = False
    failure = None
    cleanup_notes = []

    def fail(message, route, detail):
        nonlocal failure
        if failure is None:
            failure = {"message": message, "route": route, "detail": detail}

    try:
        status, imported = run_bounded(
            [*docker, "import", "--change", 'CMD ["true"]', "-"],
            input_bytes=rootfs_tar(),
        )
        if status == "timeout":
            fail(
                "selected docker command timed out",
                "the selected docker did not answer within %d seconds. This script does not switch engines and does not pull an image." % PROBE_TIMEOUT_SECONDS,
                "docker import timed out",
            )
        elif imported.returncode != 0:
            fail(
                "selected docker could not import the local mount-probe image",
                "use the selected docker CLI with a local image import and re-run. This script does not pull an image and does not switch engines.",
                last_line(decode_text(imported.stderr) + "\n" + decode_text(imported.stdout)),
            )
        else:
            image_id = received_id(imported.stdout, "image")
            if image_id is None:
                fail(
                    "selected docker could not import the local mount-probe image",
                    "use the selected docker CLI with a local image import and re-run. This script does not pull an image and does not switch engines.",
                    "docker import did not return an image id",
                )
        if failure is None:
            status, tagged = run_bounded(
                [*docker, "image", "inspect", "--format", "{{json .RepoTags}}", image_id],
            )
            if status == "timeout":
                fail(
                    "selected docker command timed out",
                    "the selected docker did not answer within %d seconds. This script does not switch engines and does not pull an image." % PROBE_TIMEOUT_SECONDS,
                    "docker image inspect timed out",
                )
                image_id = None
            else:
                tags = decode_text(tagged.stdout).strip()
                if tagged.returncode != 0 or tags not in ("[]", "null"):
                    fail(
                        "selected docker mount probe could not verify image ownership",
                        "use the selected docker CLI and re-run. This script does not delete an image it did not import.",
                        "imported image is not an untagged image id this run received",
                    )
                    image_id = None
                else:
                    image_owned = True
        if failure is None:
            mount = "type=bind,source=%s,target=/probe,readonly" % token
            status, created = run_bounded(
                [
                    *docker, "create", "--pull", "never", "--network", "none",
                    "--mount", mount, image_id, INERT_COMMAND,
                ],
            )
            if status == "timeout":
                fail(
                    "selected docker command timed out",
                    "the selected docker did not answer within %d seconds. This script does not switch engines and does not pull an image." % PROBE_TIMEOUT_SECONDS,
                    "docker create timed out",
                )
            else:
                combined = decode_text(created.stderr) + "\n" + decode_text(created.stdout)
                if created.returncode != 0:
                    if "bind source path does not exist" in combined:
                        fail(
                            "selected docker daemon cannot bind-mount this machine's files",
                            "select a daemon that bind-mounts this machine's filesystem and re-run. This script does not switch engines and does not require a historical socket path.",
                            "bind source path does not exist",
                        )
                    elif "no command specified" in combined:
                        fail(
                            "selected docker daemon rejected the mount probe create",
                            "use a daemon that accepts an inert create command for a container that is not started. This script does not switch engines.",
                            "no command specified",
                        )
                    else:
                        fail(
                            "selected docker daemon mount probe failed",
                            "use a daemon that accepts a readonly bind mount of this state directory and re-run. This script does not switch engines.",
                            last_line(combined),
                        )
                else:
                    container_id = received_id(created.stdout, "container")
                    if container_id is None:
                        fail(
                            "selected docker daemon mount probe failed",
                            "use a daemon that accepts a readonly bind mount of this state directory and re-run. This script does not switch engines.",
                            "docker create did not return a container id",
                        )
        if failure is None:
            status, inspected = run_bounded(
                [*docker, "inspect", "--format", "{{.Id}} {{.Image}} {{.State.Running}}", container_id],
            )
            if status == "timeout":
                fail(
                    "selected docker command timed out",
                    "the selected docker did not answer within %d seconds. This script does not switch engines and does not pull an image." % PROBE_TIMEOUT_SECONDS,
                    "docker inspect timed out",
                )
                container_id = None
            else:
                fields = decode_text(inspected.stdout).strip().split()
                owned = (
                    inspected.returncode == 0
                    and len(fields) == 3
                    and same_id(fields[0], container_id)
                    and same_id(fields[1], image_id)
                    and fields[2] == "false"
                )
                if not owned:
                    fail(
                        "selected docker mount probe could not verify container ownership",
                        "use the selected docker CLI and re-run. This script does not delete a container it did not create.",
                        "created container identity did not match the ids this run received",
                    )
                    container_id = None
                else:
                    container_owned = True
    finally:
        token.unlink(missing_ok=True)
        if container_owned and container_id:
            status, removed = run_bounded([*docker, "rm", container_id])
            if status == "timeout" or removed.returncode != 0:
                cleanup_notes.append("cleanup of container " + container_id + " failed")
        if image_owned and image_id:
            status, removed = run_bounded([*docker, "image", "rm", image_id])
            if status == "timeout" or removed.returncode != 0:
                cleanup_notes.append("cleanup of image " + image_id + " failed")
    if failure:
        if failure["detail"]:
            print("detail: " + failure["detail"])
        for note in cleanup_notes:
            print("detail: " + note)
        refuse(failure["message"], failure["route"])
    if cleanup_notes:
        for note in cleanup_notes:
            print("detail: " + note)
        refuse(
            "selected docker mount probe cleanup failed",
            "remove only the container and image ids this run created, then re-run. This script does not switch engines.",
        )
    print("daemon_mount=visible")


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
