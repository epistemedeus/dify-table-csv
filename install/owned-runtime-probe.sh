#!/usr/bin/env bash
# Prove owned cleanup, tracked export, and protected nat with busybox.
# This does not pull Dify.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="busybox@sha256:f97baa533a26513c453a362be331a43eb60214302f449c16571b23cea141dce4"
if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "probe_refused=docker"
  exit 2
fi

work="$(mktemp -d /tmp/dify-table-probe-XXXXXX)"
created_a=0
created_b=0
created_net=0
created_mount=0
mount_cid=""
probe_ns=""
probe_veth=""
cleanup() {
  ec=$?
  set +e
  if [[ -n "$mount_cid" ]]; then
    "${DOCKER[@]}" rm -f "$mount_cid" >/dev/null 2>&1 || true
  fi
  if [[ "$created_mount" == "1" ]]; then
    "${DOCKER[@]}" compose -p difytablecsv-ccc5ccc5 -f "$work/mount.yml" down -t 0 >/dev/null 2>&1 || true
  fi
  if [[ "$created_net" == "1" ]]; then
    "${DOCKER[@]}" compose -p difytablecsv-ccc3ccc3 -f "$work/net.yml" down -t 0 >/dev/null 2>&1 || true
  fi
  if [[ "$created_a" == "1" ]]; then
    "${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" down -t 0 >/dev/null 2>&1 || true
  fi
  if [[ "$created_b" == "1" ]]; then
    "${DOCKER[@]}" compose -p difytablecsv-bbbbbbbb -f "$work/compose.yml" down -t 0 >/dev/null 2>&1 || true
  fi
  if [[ -n "$probe_veth" ]]; then
    sudo -n ip link del "$probe_veth" >/dev/null 2>&1 || true
  fi
  if [[ -n "$probe_ns" ]]; then
    sudo -n ip netns del "$probe_ns" >/dev/null 2>&1 || true
  fi
  # Docker creates missing bind sources as root. Remove only this probe directory.
  if [[ -n "${work:-}" && -d "$work" ]]; then
    sudo -n rm -rf "$work" || rm -rf "$work" || true
  fi
  exit "$ec"
}
trap cleanup EXIT

for project in difytablecsv-aaaaaaaa difytablecsv-bbbbbbbb difytablecsv-ccc3ccc3 difytablecsv-ccc5ccc5; do
  if [[ -n "$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=${project}")" ]]; then
    echo "probe_refused=foreign_project_present"
    exit 2
  fi
done

marker="$(mktemp -d "$work/foreign-state-XXXXXX")"
printf 'UNCHANGED_STATE_MARKER\n' > "$marker/marker"
chmod 755 "$marker"
before_mode="$(stat -c %a "$marker")"
before_hash="$(sha256sum "$marker/marker")"
set +e
DIFY_TABLE_ADMIT=1 DIFY_TABLE_STATE="$marker" bash "$ROOT/install/cold-install.sh" > "$work/foreign-state.log" 2>&1
foreign_ec=$?
set -e
test "$foreign_ec" = 2
grep -q 'refuse: state directory already exists' "$work/foreign-state.log"
test "$(stat -c %a "$marker")" = "$before_mode"
test "$(sha256sum "$marker/marker")" = "$before_hash"
echo "foreign_state_refused=1"

sleep_bin="$(command -v sleep)"
cp "$sleep_bin" "$work/reader_boundary.mjs"
chmod 755 "$work/reader_boundary.mjs"
"$work/reader_boundary.mjs" 300 &
sentinel_pid=$!
owned_stop="$(mktemp -d "$work/owned-stop-XXXXXX")"
chmod 700 "$owned_stop"
python3 - "$owned_stop" "$sentinel_pid" << 'PY'
import json, os, stat, sys
state, pid = sys.argv[1], sys.argv[2]
raw = open("/proc/%s/stat" % pid, encoding="utf-8").read()
fields = raw[raw.rfind(")") + 2:].split()
start = fields[19]
st = os.lstat(state)
token = "probe-token"
doc = {
    "schema": "dify-table-csv.owned-runtime.v1",
    "owned_by": "dify-table-csv",
    "token": token,
    "state_dev": st.st_dev,
    "state_ino": st.st_ino,
    "project": "difytablecsv-ccccccc1",
    "reader_pid": int(pid),
    "reader_starttime": "1",
    "reader_boot_id": open("/proc/sys/kernel/random/boot_id", encoding="utf-8").read().strip(),
    "reader_container": "",
    "container_ids": [],
    "network_ids": [],
}
open(os.path.join(state, "owner.json"), "w").write(json.dumps(doc) + "\n")
os.chmod(os.path.join(state, "owner.json"), stat.S_IRUSR | stat.S_IWUSR)
open(os.path.join(state, "owner.token"), "w").write(token + "\n")
os.chmod(os.path.join(state, "owner.token"), stat.S_IRUSR | stat.S_IWUSR)
open(os.path.join(state, "reader.pid"), "w").write(pid + "\n")
open(os.path.join(state, "marker"), "w").write("UNCHANGED_STATE_MARKER\n")
PY
set +e
bash "$ROOT/install/stop-owned-runtime.sh" "$owned_stop" > "$work/reused.log" 2>&1
reused_ec=$?
set -e
test "$reused_ec" = 0
grep -q '^reader_pid_reused=1$' "$work/reused.log"
kill -0 "$sentinel_pid"
test "$(cat "$owned_stop/marker")" = "UNCHANGED_STATE_MARKER"
foreign_dir="$(mktemp -d "$work/foreign-owner-XXXXXX")"
cp "$owned_stop/owner.json" "$foreign_dir/owner.json"
cp "$owned_stop/owner.token" "$foreign_dir/owner.token"
printf 'UNCHANGED_STATE_MARKER\n' > "$foreign_dir/marker"
set +e
bash "$ROOT/install/stop-owned-runtime.sh" "$foreign_dir" > "$work/foreign-owner.log" 2>&1
foreign_owner_ec=$?
set -e
test "$foreign_owner_ec" = 2
grep -q 'refuse:' "$work/foreign-owner.log"
kill -0 "$sentinel_pid"
test "$(cat "$foreign_dir/marker")" = "UNCHANGED_STATE_MARKER"
kill "$sentinel_pid"
wait "$sentinel_pid" 2>/dev/null || true
echo "sentinel_pid_refused=1"

"$work/reader_boundary.mjs" 300 &
match_pid=$!
python3 - "$owned_stop" "$match_pid" << 'PY'
import json, os, sys
state, pid = sys.argv[1], sys.argv[2]
raw = open("/proc/%s/stat" % pid, encoding="utf-8").read()
doc = json.loads(open(os.path.join(state, "owner.json"), encoding="utf-8").read())
doc["reader_pid"] = int(pid)
doc["reader_starttime"] = raw[raw.rfind(")") + 2:].split()[19]
open(os.path.join(state, "owner.json"), "w").write(json.dumps(doc) + "\n")
open(os.path.join(state, "reader.pid"), "w").write(pid + "\n")
PY
bash "$ROOT/install/stop-owned-runtime.sh" "$owned_stop" > "$work/matched.log"
grep -q '^reader_stopped=1$' "$work/matched.log"
if kill -0 "$match_pid" 2>/dev/null; then
  echo "probe_refused=matched_reader_still_alive"
  exit 1
fi
test "$(cat "$owned_stop/marker")" = "UNCHANGED_STATE_MARKER"
echo "matched_reader_stopped=1"

fake="$(mktemp -d "$work/source-XXXXXX")"
mkdir -p "$fake/docker/volumes/db/data" "$fake/docker/volumes/sandbox/conf" "$work/outside"
printf 'LIVE_SENTINEL\n' > "$fake/docker/volumes/db/data/LIVE_SENTINEL"
printf 'OUTSIDE_SECRET_BYTES\n' > "$work/outside/secret"
mkdir -p "$fake/docker/volumes/app"
ln -s "$work/outside" "$fake/docker/volumes/app/storage"
printf 'SECRET_MARKER=do-not-touch\n' > "$fake/docker/.env"
printf 'SECRET_MARKER=example\n' > "$fake/docker/.env.example"
printf 'services: {}\n' > "$fake/docker/docker-compose.yaml"
printf 'max_workers: 4\nworker_timeout: 5\n' > "$fake/docker/volumes/sandbox/conf/config.yaml"
git -C "$fake" init -q
git -C "$fake" config user.email probe@example.com
git -C "$fake" config user.name Probe
git -C "$fake" add docker/docker-compose.yaml docker/.env.example docker/volumes/sandbox/conf/config.yaml
git -C "$fake" -c commit.gpgsign=false commit -q -m init
fake_head="$(git -C "$fake" rev-parse HEAD)"
before="$(sha256sum "$fake/docker/.env" "$fake/docker/docker-compose.yaml" "$fake/docker/volumes/db/data/LIVE_SENTINEL" "$work/outside/secret")"
link_before="$(readlink "$fake/docker/volumes/app/storage")"
bash "$ROOT/install/export-tracked-docker.sh" "$fake" "$work/exported" "$fake_head" > "$work/export.log"
grep -q '^tracked_export=1$' "$work/export.log"
test -f "$work/exported/docker/docker-compose.yaml"
test ! -e "$work/exported/docker/volumes/db/data/LIVE_SENTINEL"
test ! -e "$work/exported/docker/volumes/app/storage"
test ! -e "$work/exported/docker/.env"
if grep -R -q 'OUTSIDE_SECRET_BYTES\|LIVE_SENTINEL' "$work/exported"; then
  echo "probe_refused=export_followed_link"
  exit 1
fi
test "$(sha256sum "$fake/docker/.env" "$fake/docker/docker-compose.yaml" "$fake/docker/volumes/db/data/LIVE_SENTINEL" "$work/outside/secret")" = "$before"
test "$(readlink "$fake/docker/volumes/app/storage")" = "$link_before"
linked="$(mktemp -d "$work/linked-XXXXXX")"
mkdir -p "$linked/docker"
printf 'services: {}\n' > "$linked/docker/docker-compose.yaml"
ln -s "$work/outside/secret" "$linked/docker/leak"
git -C "$linked" init -q
git -C "$linked" config user.email probe@example.com
git -C "$linked" config user.name Probe
git -C "$linked" add docker
git -C "$linked" -c commit.gpgsign=false commit -q -m init
set +e
bash "$ROOT/install/export-tracked-docker.sh" "$linked" "$work/linked-out" "$(git -C "$linked" rev-parse HEAD)" > "$work/linked.log" 2>&1
linked_ec=$?
set -e
test "$linked_ec" = 2
grep -q 'refuse: tracked tree contains a symlink' "$work/linked.log"
test ! -e "$work/linked-out"
echo "tracked_export_refused_link=1"

"${DOCKER[@]}" pull "$IMAGE" >/dev/null
cat > "$work/mount.yml" << EOF
services:
  box:
    image: ${IMAGE}
    command: ["sleep", "60"]
    volumes:
      - ${work}/exported/docker/volumes/db/data:/var/lib/postgresql/data
      - ${work}/exported/docker/volumes/app/storage:/app/storage
EOF
"${DOCKER[@]}" compose -p difytablecsv-ccc5ccc5 -f "$work/mount.yml" up -d
created_mount=1
mount_cid="$("${DOCKER[@]}" compose -p difytablecsv-ccc5ccc5 -f "$work/mount.yml" ps -q)"
test -n "$mount_cid"
mount_sources="$("${DOCKER[@]}" inspect -f '{{range .Mounts}}{{.Type}} {{.Source}}{{"\n"}}{{end}}' "$mount_cid")"
test -n "$mount_sources"
while read -r kind source; do
  [[ -z "$kind" ]] && continue
  if [[ "$kind" == "bind" && "$source" != "$work/exported/"* ]]; then
    echo "probe_refused=bind_escapes_export"
    exit 1
  fi
done <<< "$mount_sources"
echo "owned_mounts=1"
if "${DOCKER[@]}" exec "$mount_cid" sh -c 'ls /var/lib/postgresql/data /app/storage 2>/dev/null | grep -q LIVE_SENTINEL'; then
  echo "probe_refused=mounted_live_sentinel"
  exit 1
fi
if "${DOCKER[@]}" exec "$mount_cid" sh -c 'grep -R OUTSIDE_SECRET_BYTES /var/lib/postgresql/data /app/storage >/dev/null 2>&1'; then
  echo "probe_refused=mounted_outside_secret"
  exit 1
fi
echo "owned_mount_runtime=1"
"${DOCKER[@]}" compose -p difytablecsv-ccc5ccc5 -f "$work/mount.yml" down >/dev/null
created_mount=0
mount_cid=""

cat > "$work/net.yml" << EOF
services:
  listen:
    image: ${IMAGE}
    command: ["sh", "-c", "while true; do printf '%s\n' same-bridge-open | nc -l -p 5432; done"]
    networks: [owned]
networks:
  owned:
    driver: bridge
    driver_opts:
      com.docker.network.bridge.gateway_mode_ipv4: nat
EOF
"${DOCKER[@]}" compose -p difytablecsv-ccc3ccc3 -f "$work/net.yml" up -d
created_net=1
listen_id="$("${DOCKER[@]}" compose -p difytablecsv-ccc3ccc3 -f "$work/net.yml" ps -q)"
test -n "$listen_id"
mode="$("${DOCKER[@]}" network inspect -f '{{index .Options "com.docker.network.bridge.gateway_mode_ipv4"}}' difytablecsv-ccc3ccc3_owned)"
test "$mode" = "nat"
echo "owned_gateway_mode=nat"
listen_ip="$("${DOCKER[@]}" inspect -f '{{(index .NetworkSettings.Networks "difytablecsv-ccc3ccc3_owned").IPAddress}}' "$listen_id")"
test -n "$listen_ip"
# A busybox nc that connects and then exits looks the same as a refusal if the
# only signal is "still running after two seconds". Require the listener token.
same_bridge_token=""
same_bridge_open() {
  local attempt
  for attempt in 1 2 3 4 5; do
    same_bridge_token="$("${DOCKER[@]}" run --rm --network "$1" "$IMAGE" nc -w 3 "$2" "$3" 2>"$work/same-bridge.err" || true)"
    if printf '%s\n' "$same_bridge_token" | grep -q 'same-bridge-open'; then
      return 0
    fi
    sleep 0.4
  done
  return 1
}
if ! same_bridge_open difytablecsv-ccc3ccc3_owned "$listen_ip" 5432; then
  "${DOCKER[@]}" logs "$listen_id" > "$work/listen.log" 2>&1 || true
  echo "probe_refused=same_bridge_unpublished_closed"
  printf 'same_bridge_client=%s\n' "$same_bridge_token"
  cat "$work/same-bridge.err" || true
  cat "$work/listen.log" || true
  exit 1
fi
echo "same_bridge_unpublished_open=1"
subnet="$("${DOCKER[@]}" network inspect -f '{{(index .IPAM.Config 0).Subnet}}' difytablecsv-ccc3ccc3_owned)"
bridge_id="$("${DOCKER[@]}" network inspect -f '{{.Id}}' difytablecsv-ccc3ccc3_owned)"
bridge_if="br-${bridge_id:0:12}"
# An unpublished port has no raw PREROUTING rule. nat installs
# `-A DOCKER ! -i <bridge> -o <bridge> -j DROP`. nat-unprotected installs ACCEPT.
filter_drop="$(sudo -n iptables -S DOCKER | grep -F -- "-A DOCKER ! -i ${bridge_if} -o ${bridge_if} -j DROP" || true)"
filter_accept="$(sudo -n iptables -S DOCKER | grep -F -- "-A DOCKER ! -i ${bridge_if} -o ${bridge_if} -j ACCEPT" || true)"
if [[ -z "$filter_drop" || -n "$filter_accept" ]]; then
  echo "filter_docker_drop=0"
  echo "owned_bridge=${bridge_if}"
  ip link show "$bridge_if" || true
  sudo -n iptables -S DOCKER || true
  exit 1
fi
echo "filter_docker_drop=1"
suffix="$(od -An -N3 -tx1 /dev/urandom | tr -d ' \n')"
probe_ns="dtcsv${suffix}"
probe_veth="vethh${suffix}"
probe_peer="vethn${suffix}"
sudo -n ip netns add "$probe_ns"
sudo -n ip link add "$probe_veth" type veth peer name "$probe_peer"
sudo -n ip link set "$probe_peer" netns "$probe_ns"
sudo -n ip addr add 192.0.2.8/30 dev "$probe_veth"
sudo -n ip link set "$probe_veth" up
sudo -n ip netns exec "$probe_ns" ip addr add 192.0.2.9/30 dev "$probe_peer"
sudo -n ip netns exec "$probe_ns" ip link set "$probe_peer" up
sudo -n ip netns exec "$probe_ns" ip link set lo up
sudo -n ip netns exec "$probe_ns" ip route add "$subnet" via 192.0.2.8
set +e
sudo -n ip netns exec "$probe_ns" python3 - "$listen_ip" << 'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(3)
try:
    sock.connect((sys.argv[1], 5432))
except socket.timeout:
    raise SystemExit(7)
except OSError as exc:
    print("%s" % exc)
    raise SystemExit(8)
else:
    raise SystemExit(0)
PY
boundary_ec=$?
set -e
if [[ "$boundary_ec" != "7" ]]; then
  echo "cross_boundary_unpublished_refused=0"
  echo "cross_boundary_exit=${boundary_ec}"
  exit 1
fi
echo "cross_boundary_unpublished_refused=1"
echo "negative_proves=Direct TCP from a network namespace that has a route to the unpublished listener, arriving on a veth that is not the owned bridge, did not open that port. The owned network inspect value is gateway mode nat, and its filter DOCKER rule drops packets forwarded onto that bridge from any other interface. A same-bridge client opened the port. This does not prove the host has no internet and does not prove a loopback-published port is closed."
sudo -n ip link del "$probe_veth" >/dev/null 2>&1 || true
probe_veth=""
sudo -n ip netns del "$probe_ns" >/dev/null 2>&1 || true
probe_ns=""
"${DOCKER[@]}" compose -p difytablecsv-ccc3ccc3 -f "$work/net.yml" down >/dev/null
created_net=0

cat > "$work/compose.yml" << EOF
services:
  sentinel:
    image: ${IMAGE}
    command: ["sleep", "180"]
EOF
"${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" up -d
created_a=1
"${DOCKER[@]}" compose -p difytablecsv-bbbbbbbb -f "$work/compose.yml" up -d
created_b=1
foreign_id="$("${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" ps -q)"
owned_b="$("${DOCKER[@]}" compose -p difytablecsv-bbbbbbbb -f "$work/compose.yml" ps -q)"
if [[ -z "$foreign_id" || -z "$owned_b" ]]; then
  echo "probe_refused=sentinel_id"
  "${DOCKER[@]}" ps -a --filter name=difytablecsv- --format '{{.ID}} {{.Names}} {{.Status}}' || true
  exit 1
fi

echo "probe_phase=collision"
set +e
DIFY_TABLE_ADMIT=1 DIFY_TABLE_PROJECT=difytablecsv-aaaaaaaa DIFY_SOURCE="$fake" \
  bash "$ROOT/install/cold-install.sh" > "$work/collision.log" 2>&1
collision_ec=$?
set -e
if [[ "$collision_ec" != "2" ]] || ! grep -q '^collision_refused=1$' "$work/collision.log"; then
  echo "probe_refused=collision"
  echo "collision_exit=${collision_ec}"
  cat "$work/collision.log" || true
  exit 1
fi
after="$(sha256sum "$fake/docker/.env" "$fake/docker/docker-compose.yaml" "$fake/docker/volumes/db/data/LIVE_SENTINEL" "$work/outside/secret")"
test "$before" = "$after"
test "$("${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" ps -q)" = "$foreign_id"

echo "probe_phase=head"
set +e
DIFY_TABLE_ADMIT=1 DIFY_SOURCE="$fake" bash "$ROOT/install/cold-install.sh" > "$work/head.log" 2>&1
head_ec=$?
set -e
if [[ "$head_ec" != "2" ]] || ! grep -q 'refuse: dify source head' "$work/head.log"; then
  echo "probe_refused=head"
  echo "head_exit=${head_ec}"
  cat "$work/head.log" || true
  exit 1
fi
test "$(sha256sum "$fake/docker/.env" "$fake/docker/docker-compose.yaml" "$fake/docker/volumes/db/data/LIVE_SENTINEL" "$work/outside/secret")" = "$before"
if grep -q '^owned_project=' "$work/head.log"; then
  created="$(sed -n 's/^owned_project=//p' "$work/head.log" | head -1)"
  test -z "$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=${created}")"
fi

b_state="$(mktemp -d "$work/project-b-XXXXXX")"
chmod 700 "$b_state"
full_b="$("${DOCKER[@]}" inspect -f '{{.Id}}' "$owned_b")"
"${DOCKER[@]}" network ls -q --no-trunc --filter label=com.docker.compose.project=difytablecsv-bbbbbbbb > "$work/b.networks"
python3 - "$b_state" "$full_b" "$work/b.networks" << 'PY'
import json, os, stat, sys
state, container, net_path = sys.argv[1:]
st = os.lstat(state)
token = "probe-b-token"
doc = {
    "schema": "dify-table-csv.owned-runtime.v1",
    "owned_by": "dify-table-csv",
    "token": token,
    "state_dev": st.st_dev,
    "state_ino": st.st_ino,
    "project": "difytablecsv-bbbbbbbb",
    "reader_container": "",
    "container_ids": [],
    "network_ids": [],
}
open(os.path.join(state, "owner.json"), "w").write(json.dumps(doc) + "\n")
os.chmod(os.path.join(state, "owner.json"), stat.S_IRUSR | stat.S_IWUSR)
open(os.path.join(state, "owner.token"), "w").write(token + "\n")
os.chmod(os.path.join(state, "owner.token"), stat.S_IRUSR | stat.S_IWUSR)
PY
echo "probe_phase=foreign_container"
set +e
bash "$ROOT/install/stop-owned-runtime.sh" "$b_state" > "$work/foreign-container.log" 2>&1
foreign_container_ec=$?
set -e
if [[ "$foreign_container_ec" != "2" ]] || ! grep -q '^stop_refused=foreign_container$' "$work/foreign-container.log"; then
  echo "probe_refused=foreign_container"
  echo "foreign_container_exit=${foreign_container_ec}"
  cat "$work/foreign-container.log" || true
  exit 1
fi
test "$("${DOCKER[@]}" compose -p difytablecsv-bbbbbbbb -f "$work/compose.yml" ps -q)" = "$owned_b"
python3 - "$b_state" "$full_b" "$work/b.networks" << 'PY'
import json, os, sys
state, container, net_path = sys.argv[1:]
doc = json.loads(open(os.path.join(state, "owner.json"), encoding="utf-8").read())
doc["container_ids"] = [container]
doc["network_ids"] = [line.strip() for line in open(net_path, encoding="utf-8") if line.strip()]
open(os.path.join(state, "owner.json"), "w").write(json.dumps(doc) + "\n")
PY
echo "probe_phase=stop_owned"
bash "$ROOT/install/stop-owned-runtime.sh" "$b_state" > "$work/stop-b.log"
if ! grep -q '^stack_stopped=1$' "$work/stop-b.log"; then
  echo "probe_refused=stop_owned"
  cat "$work/stop-b.log" || true
  exit 1
fi
test -z "$("${DOCKER[@]}" compose -p difytablecsv-bbbbbbbb -f "$work/compose.yml" ps -q)"
test "$("${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" ps -q)" = "$foreign_id"
created_b=0
"${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" down -t 0 >/dev/null
created_a=0
test -z "$("${DOCKER[@]}" compose -p difytablecsv-aaaaaaaa -f "$work/compose.yml" ps -q)"
echo "owned_probe=passed"
