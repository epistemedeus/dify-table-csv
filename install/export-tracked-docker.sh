#!/usr/bin/env bash
# Export raw blobs from one exact Git commit. Worktree data and archive attributes are not inputs.
set -euo pipefail
src="${1:?}"
dest="${2:?}"
commit="${3:?}"
if [[ -L "$dest" || -e "$dest" ]]; then
  echo "refuse: export destination exists"
  exit 2
fi
parent="$(dirname "$dest")"
if [[ -L "$parent" || ! -d "$parent" ]]; then
  echo "refuse: export parent is not a directory"
  exit 2
fi
stage="$(mktemp -d "${parent}/.export-XXXXXX")"
cleanup_stage() { rm -rf "$stage"; }
trap cleanup_stage EXIT
python3 - "$src" "$stage" "$commit" << 'PY'
import hashlib, os, pathlib, re, subprocess, sys
src, stage, commit = sys.argv[1:]
def fail(message):
    print(message)
    raise SystemExit(2)
def git(*args):
    result = subprocess.run(["git", "--no-replace-objects", "-C", src, *args], capture_output=True)
    if result.returncode:
        fail("refuse: tracked docker export failed")
    return result.stdout
def verified(kind, oid):
    body = git("cat-file", kind, oid)
    actual = hashlib.sha1(kind.encode() + b" " + str(len(body)).encode() + b"\0" + body).hexdigest()
    if actual != oid:
        fail("refuse: tracked object hash differs")
    return body
if not re.fullmatch(r"[a-f0-9]{40}", commit):
    fail("refuse: exact commit required")
body = verified("commit", commit)
first = body.split(b"\n", 1)[0]
if not re.fullmatch(rb"tree [a-f0-9]{40}", first):
    fail("refuse: commit tree is invalid")
tree = first[5:].decode("ascii")
verified("tree", tree)
entries = git("ls-tree", "--full-tree", "-r", "-z", tree, "--", "docker").split(b"\0")
members = []
for entry in filter(None, entries):
    metadata, separator, raw_path = entry.partition(b"\t")
    fields = metadata.split()
    if not separator or len(fields) != 3:
        fail("refuse: tracked tree entry is invalid")
    mode, kind, oid = fields
    if mode == b"120000":
        fail("refuse: tracked tree contains a symlink")
    if mode not in (b"100644", b"100755") or kind != b"blob" or not re.fullmatch(rb"[a-f0-9]{40}", oid):
        fail("refuse: tracked tree contains an unsupported member")
    path = pathlib.PurePosixPath(os.fsdecode(raw_path))
    if path.is_absolute() or not path.parts or path.parts[0] != "docker" or any(p in ("", ".", "..") for p in path.parts):
        fail("refuse: tracked path escapes the export")
    members.append((path, mode, oid.decode("ascii")))
if not members:
    fail("refuse: tracked docker tree is empty")
for path, mode, oid in members:
    target = pathlib.Path(stage, *path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        handle.write(verified("blob", oid))
    target.chmod(0o755 if mode == b"100755" else 0o644)
print("tracked_export=1")
PY
mv "$stage" "$dest"
trap - EXIT
