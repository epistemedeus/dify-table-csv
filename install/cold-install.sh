#!/usr/bin/env bash
# Disposable Dify 1.18.0 acceptance run for the packed table workflow.
# Without DIFY_TABLE_ADMIT=1 or H64314_ADMIT_DIFY=1 it does not docker pull
# and does not start containers. A supplied Dify checkout is read only.
# Normal CSV commands are src/direct_csv.py and install/use-existing.py.
# State is an exclusively created directory outside this package source.
# Cleanup is install/stop-owned-runtime.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIFY_COMMIT="50abea97062110044f15d0e03ccecadd9ccc87ca"
LICENSE_SHA="232cf91474932d5110ed304e53b6b742a58463857c571fae803fdf2ac36d7bb3"
EXTRACT_SHA="e087cdcc6bbb019fa9ea0b8ca2e5f7d9e682dfb2bfd6b205e119330d32a5c6e1"
LOCK_SHA="fff2da9d1138eb6d6aab7718b797dee9e342366f69779ab9f7cd788421a35698"
echo "installed_admission_boundary"
echo "minimal_services=api,worker,db_postgres,redis,sandbox,ssrf_proxy,plugin_daemon,init_permissions"
echo "omitted=weaviate,web,nginx,agent_backend,local_sandbox,agent_ssrf_proxy,worker_beat,api_websocket"
echo "dify_commit=${DIFY_COMMIT}"
echo "network_gateway_mode=nat"
if [[ -r /proc/meminfo ]]; then
  mem="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
  echo "mem_available_kib=${mem}"
fi
echo "H64314_ADMIT_DIFY=${H64314_ADMIT_DIFY:-unset}"
echo "DIFY_TABLE_ADMIT=${DIFY_TABLE_ADMIT:-unset}"
if [[ "${DIFY_TABLE_ADMIT:-}" != "1" && "${H64314_ADMIT_DIFY:-}" != "1" ]]; then
  echo "not_installed"
  echo "refuse: admission flag is unset"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi

if [[ -n "${DIFY_TABLE_PROJECT:-}" ]]; then
  PROJECT="$DIFY_TABLE_PROJECT"
  if [[ ! "$PROJECT" =~ ^difytablecsv-[a-f0-9]{8}$ ]]; then
    echo "not_installed"
    echo "refuse: project name"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
else
  hex="$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  PROJECT="difytablecsv-${hex}"
fi

# Canonical source and candidate, including existing symlink ancestors.
# Exit 3 means the state root is the package source or a path inside it.
state_is_inside_source() {
  local candidate="$1"
  local rc=0
  python3 "$ROOT/install/state-boundary.py" "$ROOT" "$candidate" "$(pwd -P)" >/dev/null || rc=$?
  if [[ "$rc" == "3" ]]; then
    return 0
  fi
  if [[ "$rc" == "0" ]]; then
    return 1
  fi
  echo "not_installed"
  echo "refuse: state directory is not a usable path"
  echo "This script does not docker pull and does not start compose."
  exit 2
}

if [[ -n "${DIFY_TABLE_STATE:-}" ]]; then
  state="$DIFY_TABLE_STATE"
  if state_is_inside_source "$state"; then
    echo "not_installed"
    echo "refuse: state directory is inside the package source"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
  if [[ -L "$state" || -e "$state" ]]; then
    echo "not_installed"
    echo "refuse: state directory already exists"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
  parent="$(dirname "$state")"
  if [[ -L "$parent" || ! -d "$parent" ]]; then
    echo "not_installed"
    echo "refuse: state parent is not a directory"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
  if ! mkdir "$state"; then
    echo "not_installed"
    echo "refuse: state directory already exists"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
else
  probe="$(mktemp -u /tmp/dify-table-state-XXXXXX)"
  if state_is_inside_source "$probe"; then
    echo "not_installed"
    echo "refuse: state directory is inside the package source"
    echo "This script does not docker pull and does not start compose."
    exit 2
  fi
  state="$(mktemp -d /tmp/dify-table-state-XXXXXX)"
fi
chmod 700 "$state"
echo "state_dir=${state}"
echo "owned_project=${PROJECT}"

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  echo "not_installed"
  echo "refuse: docker daemon is not reachable"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi

refuse_collision() {
  echo "not_installed"
  echo "collision_refused=1"
  echo "refuse: project ${PROJECT} already has containers or networks"
  echo "This script does not docker pull and does not start compose."
  exit 2
}

existing="$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=${PROJECT}")"
if [[ -n "$existing" ]]; then
  refuse_collision
fi
for net in "${PROJECT}_default" "${PROJECT}_ssrf_proxy_network"; do
  if "${DOCKER[@]}" network inspect "$net" >/dev/null 2>&1; then
    refuse_collision
  fi
done

port_open() {
  python3 - "$1" << 'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
try:
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
finally:
    sock.close()
PY
}
if port_open 5001; then
  echo "not_installed"
  echo "refuse: 127.0.0.1:5001 is already accepting connections"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi
if port_open 8765; then
  echo "not_installed"
  echo "refuse: 127.0.0.1:8765 is already accepting connections"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi

src="${DIFY_SOURCE:-${H64314_DIFY_SRC:-}}"
if [[ -z "$src" ]]; then
  src="${state}/upstream"
  git init "$src"
  git -C "$src" remote add origin https://github.com/langgenius/dify.git
  git -C "$src" sparse-checkout init --cone
  git -C "$src" sparse-checkout set docker
  git -C "$src" fetch --depth 1 --filter=blob:none origin "$DIFY_COMMIT"
  git -C "$src" checkout --detach FETCH_HEAD
fi
if [[ -L "$src/docker" ]]; then
  echo "not_installed"
  echo "refuse: docker directory is a symlink"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi
if [[ -L "$src/LICENSE" ]]; then
  echo "not_installed"
  echo "refuse: LICENSE is a symlink"
  echo "This script does not docker pull and does not start compose."
  exit 2
fi

python3 - "$state/source.sha256" "$src" << 'PY'
import hashlib, os, sys
dest, src = sys.argv[1], sys.argv[2]
names = [
    "LICENSE",
    "docker/.env.example",
    "docker/docker-compose.yaml",
    "docker/volumes/sandbox/conf/config.yaml",
    "docker/.env",
]
lines = []
for rel in names:
    path = os.path.join(src, rel)
    if os.path.islink(path):
        raise SystemExit("symlink in supplied source: " + rel)
    if not os.path.isfile(path):
        continue
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    lines.append("%s  %s" % (digest, path))
open(dest, "w").write(("\n".join(lines) + "\n") if lines else "")
print("source_hashed=%d" % len(lines))
PY

compose_started=0
compose=()
reader_pid_file="${state}/reader.pid"
cleanup() {
  set +e
  if [[ -f "${state}/source.sha256" ]]; then
    python3 - "${state}/source.sha256" << 'PY'
import hashlib, pathlib, sys
manifest = pathlib.Path(sys.argv[1])
ok = True
for line in manifest.read_text().splitlines():
    if "  " not in line:
        continue
    digest, file_path = line.split("  ", 1)
    current = hashlib.sha256(pathlib.Path(file_path).read_bytes()).hexdigest()
    if current != digest:
        ok = False
        break
print("source_bytes_unchanged=%s" % (1 if ok else 0))
PY
  fi
  if [[ "$compose_started" == "1" && "${DIFY_TABLE_KEEP:-}" == "1" ]]; then
    echo "stack_kept=1"
  elif [[ -f "${state}/owner.json" && ! -L "${state}/owner.json" ]]; then
    if [[ "$compose_started" == "1" ]]; then
      "${compose[@]}" logs --tail 60 > "${state}/engine-logs-tail.txt" 2>/dev/null || true
    fi
    bash "${ROOT}/install/stop-owned-runtime.sh" "${state}" || true
  fi
}
trap cleanup EXIT

head="$(git -C "$src" rev-parse HEAD 2>/dev/null || true)"
if [[ "$head" != "$DIFY_COMMIT" ]]; then
  echo "not_installed"
  echo "refuse: dify source head is ${head}"
  exit 2
fi
echo "${LICENSE_SHA}  ${src}/LICENSE" | sha256sum -c -
echo "dify_source=${src}"

READER_CORE="${READER_CORE:-$ROOT/reader}"
if [[ ! -f "${READER_CORE}/extract.mjs" ]]; then
  echo "not_installed"
  echo "refuse: READER_CORE is not a prepared reader-core directory"
  exit 2
fi
echo "${EXTRACT_SHA}  ${READER_CORE}/extract.mjs" | sha256sum -c -
echo "${LOCK_SHA}  ${READER_CORE}/package-lock.json" | sha256sum -c -
if [[ ! -d "${READER_CORE}/node_modules/zod" ]]; then
  npm ci --ignore-scripts --prefix "$READER_CORE"
fi
export READER_CORE

bash "${ROOT}/install/export-tracked-docker.sh" "$src" "${state}/engine" "$DIFY_COMMIT"
copy_env="${state}/engine/docker/.env"
sandbox="${state}/engine/docker/volumes/sandbox/conf/config.yaml"
if [[ -L "${state}/engine/docker/.env.example" || -L "$sandbox" ]]; then
  echo "not_installed"
  echo "refuse: copied configuration is a symlink"
  exit 2
fi
if [[ -L "$copy_env" ]]; then
  rm "$copy_env"
fi
cp "${state}/engine/docker/.env.example" "$copy_env"
case "$(readlink -f "$copy_env")" in
  "$state"/*) ;;
  *)
    echo "not_installed"
    echo "refuse: env path escapes the state directory"
    exit 2
    ;;
esac
case "$(readlink -f "$sandbox")" in
  "$state"/*) ;;
  *)
    echo "not_installed"
    echo "refuse: sandbox config escapes the state directory"
    exit 2
    ;;
esac

gateway="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
if [[ -z "$gateway" ]]; then
  echo "not_installed"
  echo "refuse: docker0 gateway is unknown, so no single-host SSRF allow was written"
  exit 2
fi
export DIFY_TABLE_DOCKER0="$gateway"

python3 - "$copy_env" "${gateway}/32" "${state}/local-admin.txt" << 'PY'
import pathlib, secrets, string, sys
env_path, cidr, admin_path = sys.argv[1:]
text = pathlib.Path(env_path).read_text()
alphabet = string.ascii_letters + string.digits

def token(n):
    return "Aa1" + "".join(secrets.choice(alphabet) for _ in range(n))

init_password = token(17)
admin_password = token(17)
secret = token(32)
replacements = {
    "INIT_PASSWORD=": "INIT_PASSWORD=" + init_password,
    "SECRET_KEY=": "SECRET_KEY=" + secret,
    "COMPOSE_PROFILES=${VECTOR_STORE:-weaviate},${DB_TYPE:-postgresql},collaboration": "COMPOSE_PROFILES=postgresql",
    "CELERY_WORKER_AMOUNT=4": "CELERY_WORKER_AMOUNT=1",
    "CELERY_AUTO_SCALE=false": "CELERY_AUTO_SCALE=false",
    "SQLALCHEMY_POOL_SIZE=30": "SQLALCHEMY_POOL_SIZE=5",
    "SQLALCHEMY_MAX_OVERFLOW=10": "SQLALCHEMY_MAX_OVERFLOW=2",
    "POSTGRES_MAX_CONNECTIONS=200": "POSTGRES_MAX_CONNECTIONS=40",
    "POSTGRES_SHARED_BUFFERS=128MB": "POSTGRES_SHARED_BUFFERS=64MB",
    "POSTGRES_MAINTENANCE_WORK_MEM=64MB": "POSTGRES_MAINTENANCE_WORK_MEM=32MB",
    "POSTGRES_EFFECTIVE_CACHE_SIZE=4096MB": "POSTGRES_EFFECTIVE_CACHE_SIZE=256MB",
    "SSRF_DEFAULT_TIME_OUT=5": "SSRF_DEFAULT_TIME_OUT=30",
    "SSRF_DEFAULT_CONNECT_TIME_OUT=5": "SSRF_DEFAULT_CONNECT_TIME_OUT=10",
    "SSRF_DEFAULT_READ_TIME_OUT=5": "SSRF_DEFAULT_READ_TIME_OUT=30",
    "SSRF_DEFAULT_WRITE_TIME_OUT=5": "SSRF_DEFAULT_WRITE_TIME_OUT=30",
    "SANDBOX_WORKER_TIMEOUT=15": "SANDBOX_WORKER_TIMEOUT=30",
    "MARKETPLACE_ENABLED=true": "MARKETPLACE_ENABLED=false",
    "SSRF_PROXY_ALLOW_PRIVATE_IPS=": "SSRF_PROXY_ALLOW_PRIVATE_IPS=" + cidr,
}
for old, new in replacements.items():
    if old not in text:
        raise SystemExit("missing env line: " + old)
    text = text.replace(old, new, 1)
if "SSRF_PROXY_ALLOW_PRIVATE_DOMAINS=" not in text:
    text += "\nSSRF_PROXY_ALLOW_PRIVATE_DOMAINS=host.docker.internal\n"
admin = pathlib.Path(admin_path)
admin.write_text(
    "email=dify-table-local@example.com\nname=Dify Table Local\npassword=%s\ninit_password=%s\n" % (admin_password, init_password)
)
admin.chmod(0o600)
pathlib.Path(env_path).write_text(text)
print("local_admin_written=1")
print("ssrf_allow=" + cidr)
PY
export COMPOSE_PROFILES=postgresql

python3 - "$sandbox" << 'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text = path.read_text()
text2, n_workers = re.subn(r"^max_workers:\s*\d+\s*$", "max_workers: 1", text, count=1, flags=re.M)
text3, n_timeout = re.subn(r"^worker_timeout:\s*\d+\s*$", "worker_timeout: 30", text2, count=1, flags=re.M)
if n_workers != 1 or n_timeout != 1:
    raise SystemExit("sandbox config did not take the minimal worker settings")
path.write_text(text3)
print("sandbox_config=max_workers_1 timeout_30")
PY

engine="${state}/engine/docker/docker-compose.yaml"
squid_template="${state}/engine/docker/ssrf_proxy/squid.conf.template"
if [[ ! -f "$squid_template" || -L "$squid_template" ]]; then
  echo "not_installed"
  echo "refuse: squid template is not a regular file in the owned export"
  exit 2
fi
if ! grep -q '^hosts_file /etc/hosts$' "$squid_template"; then
  printf '\nhosts_file /etc/hosts\n' >> "$squid_template"
fi
pin="${ROOT}/install/compose.pin.yml"
env_file="$copy_env"
compose=("${DOCKER[@]}" compose -p "$PROJECT" -f "$engine" -f "$pin" --env-file "$env_file")
cat > "${state}/runtime.env" << EOF
PROJECT=${PROJECT}
ENGINE_COMPOSE=${engine}
PIN_COMPOSE=${pin}
ENV_FILE=${env_file}
OWNED_BY=dify-table-csv
EOF
python3 - "$state" "$PROJECT" "$engine" "$pin" "$env_file" << 'PY'
import json, os, secrets, stat, sys
state, project, engine, pin, env_file = sys.argv[1:]
st = os.lstat(state)
token = secrets.token_hex(32)
doc = {
    "schema": "dify-table-csv.owned-runtime.v1",
    "owned_by": "dify-table-csv",
    "token": token,
    "state_dev": st.st_dev,
    "state_ino": st.st_ino,
    "project": project,
    "engine_compose": engine,
    "pin_compose": pin,
    "env_file": env_file,
    "reader_pid": None,
    "reader_starttime": "",
    "reader_boot_id": "",
    "reader_container": "",
    "container_ids": [],
    "network_ids": [],
}
owner = os.path.join(state, "owner.json")
token_path = os.path.join(state, "owner.token")
with open(owner, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2)
    handle.write("\n")
os.chmod(owner, stat.S_IRUSR | stat.S_IWUSR)
with open(token_path, "w", encoding="utf-8") as handle:
    handle.write(token + "\n")
os.chmod(token_path, stat.S_IRUSR | stat.S_IWUSR)
print("ownership_recorded=1")
PY
DIFY_TABLE_OWNER_TOKEN="$(tr -d '[:space:]' < "${state}/owner.token")"
export DIFY_TABLE_OWNER_TOKEN

export DIFY_TABLE_ADMIT=1
export H64314_ADMIT_DIFY=1
export READER_BIND="$gateway"
export PORT=8765
export READER_PID_FILE="$reader_pid_file"
export READER_BASE="http://${gateway}:8765"
echo "reader_base=${READER_BASE}"
echo "compose_config_services:"
"${compose[@]}" config --services
"${compose[@]}" config --format json | python3 "$ROOT/install/verify-compose-hosts.py" "$gateway"

snapshot_owned() {
  "${DOCKER[@]}" ps -aq --no-trunc --filter "label=com.docker.compose.project=${PROJECT}" > "${state}/live.containers"
  "${DOCKER[@]}" network ls -q --no-trunc --filter "label=com.docker.compose.project=${PROJECT}" > "${state}/live.networks"
  python3 - "$state" << 'PY'
import json, pathlib, sys
state = sys.argv[1]
doc = json.loads(pathlib.Path(state, "owner.json").read_text())
def lines(name):
    return [line.strip() for line in pathlib.Path(state, name).read_text().splitlines() if line.strip()]
doc["container_ids"] = lines("live.containers")
doc["network_ids"] = lines("live.networks")
path = pathlib.Path(state, "owner.json")
path.write_text(json.dumps(doc, indent=2) + "\n")
path.chmod(0o600)
PY
  rm -f "${state}/live.containers" "${state}/live.networks"
}

record_host_reader() {
  reader_pid="$(tr -d '[:space:]' < "$reader_pid_file")"
  reader_start=""
  if [[ "$reader_pid" =~ ^[1-9][0-9]*$ && -r "/proc/${reader_pid}/stat" ]]; then
    reader_start="$(python3 - "$reader_pid" << 'PY'
import sys
raw = open("/proc/%s/stat" % sys.argv[1], encoding="utf-8").read()
fields = raw[raw.rfind(")") + 2:].split()
print(fields[19] if len(fields) >= 20 else "")
PY
)"
  fi
  python3 - "$state" "$reader_pid" "$reader_start" << 'PY'
import json, pathlib, sys
state, reader_pid, reader_start = sys.argv[1:]
doc = json.loads(pathlib.Path(state, "owner.json").read_text())
doc["reader_container"] = ""
if reader_pid.isdigit():
    doc["reader_pid"] = int(reader_pid)
    doc["reader_boot_id"] = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
if reader_start:
    doc["reader_starttime"] = reader_start
path = pathlib.Path(state, "owner.json")
path.write_text(json.dumps(doc, indent=2) + "\n")
path.chmod(0o600)
PY
  echo "reader_boundary_pid=${reader_pid} bind=${gateway}:8765"
}

if python3 - "$gateway" << 'PY'
import socket, sys
sock = socket.socket()
sock.settimeout(0.5)
try:
    raise SystemExit(0 if sock.connect_ex((sys.argv[1], 8765)) == 0 else 1)
finally:
    sock.close()
PY
then
  echo "not_installed"
  echo "refuse: ${gateway}:8765 is already accepting connections"
  exit 2
fi
if ! command -v node >/dev/null 2>&1; then
  echo "reader_health=failed"
  echo "refuse: node is not on PATH"
  exit 1
fi
compose_started=1
node "$ROOT/src/reader_boundary.mjs" > "${state}/reader-boundary.log" 2>&1 &
echo $! > "$reader_pid_file"
record_host_reader
reader_ready=0
for _ in $(seq 1 40); do
  if python3 - "${READER_BASE}/health" << 'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    reader_ready=1
    break
  fi
  sleep 1
done
echo "reader_health=${reader_ready}"
if [[ "$reader_ready" != "1" || ! -s "${reader_pid_file}" ]]; then
  echo "reader_health=failed"
  exit 1
fi

df -h / /var/lib/docker 2>/dev/null | awk 'NR==1 || NR==2 {print "disk " $0}' || true
echo "docker_pull_begin"
"${compose[@]}" pull api worker db_postgres redis sandbox ssrf_proxy plugin_daemon init_permissions
echo "docker_pull_end"

up_ec=0
"${compose[@]}" up -d init_permissions db_postgres redis sandbox ssrf_proxy plugin_daemon || up_ec=$?
snapshot_owned
if [[ "${up_ec}" != "0" ]]; then
  echo "api_health=failed"
  exit 1
fi
echo "waiting_for_api_dependencies"
deps_ready=0
for _ in $(seq 1 90); do
  if "${compose[@]}" ps --status running --services | grep -qx db_postgres \
    && "${compose[@]}" ps --status running --services | grep -qx redis \
    && "${compose[@]}" ps --status running --services | grep -qx sandbox \
    && "${compose[@]}" ps --status running --services | grep -qx ssrf_proxy \
    && "${compose[@]}" ps --status running --services | grep -qx plugin_daemon; then
    deps_ready=1
    break
  fi
  sleep 2
done
echo "api_dependencies_ready=${deps_ready}"
if [[ "$deps_ready" != "1" ]]; then
  "${compose[@]}" ps || true
  echo "api_health=failed"
  exit 1
fi
up_ec=0
"${compose[@]}" up -d --no-deps api || up_ec=$?
snapshot_owned
if [[ "${up_ec}" != "0" ]]; then
  echo "api_health=failed"
  exit 1
fi
echo "waiting_for_api_health"
healthy=0
for _ in $(seq 1 150); do
  if python3 - << 'PY'
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:5001/health", timeout=3) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" != "1" ]]; then
  echo "api_health=failed"
  "${compose[@]}" logs --tail 80 api > "${state}/api-health-fail.log" 2>&1 || true
  exit 1
fi
echo "api_health=ok"
version="$(python3 - << 'PY'
import urllib.request
request = urllib.request.Request("http://127.0.0.1:5001/health")
with urllib.request.urlopen(request, timeout=5) as response:
    print(response.headers.get("X-Version", ""))
PY
)"
echo "api_version=${version}"
up_ec=0
"${compose[@]}" up -d --no-deps worker || up_ec=$?
snapshot_owned
if [[ "${up_ec}" != "0" ]]; then
  echo "worker_health=failed"
  exit 1
fi
echo "waiting_for_worker"
worker_ready=0
for _ in $(seq 1 90); do
  if "${compose[@]}" logs --tail 80 worker 2>/dev/null | grep -q "celery@"; then
    worker_ready=1
    break
  fi
  sleep 2
done
echo "worker_seen=${worker_ready}"
if [[ "$worker_ready" != "1" ]]; then
  echo "worker_health=failed"
  exit 1
fi
"${compose[@]}" ps

export DIFY_CONSOLE_ORIGIN=http://127.0.0.1:5001
export DIFY_LOCAL_ADMIN_FILE="${state}/local-admin.txt"
export H64314_EVIDENCE="${state}/installed-run.json"
export PATH="${state}/bin:${PATH}"
PY="${state}/py/bin/python"
if ! python3 -m venv "${state}/py"; then
  echo "not_installed"
  echo "refuse: python venv for pyyaml==6.0.2 was not created"
  exit 1
fi
"${state}/py/bin/pip" install --disable-pip-version-check 'pyyaml==6.0.2'
"$PY" -c 'import yaml,sys; sys.exit(0 if yaml.__version__=="6.0.2" else 1)'
echo "pyyaml=6.0.2"
echo "reader_reachability_begin"
set +e
"${compose[@]}" exec -T api python - << 'PY'
import urllib.request
url = "http://host.docker.internal:8765/health"
def show(label, action):
    try:
        with action() as response:
            body = response.read(200)
        print(label + "=1")
        print(body.decode("utf-8", "replace"))
        return True
    except Exception as exc:
        print(label + "=0")
        print(type(exc).__name__ + ": " + str(exc))
        return False
direct = show("reader_from_api_direct", lambda: urllib.request.urlopen(url, timeout=5))
proxy = urllib.request.ProxyHandler({"http": "http://ssrf_proxy:3128"})
opener = urllib.request.build_opener(proxy)
proxied = show("reader_from_api_proxy", lambda: opener.open(url, timeout=5))
raise SystemExit(0 if direct and proxied else 1)
PY
reach_ec=$?
set -e
if [[ "$reach_ec" != "0" ]]; then
  echo "reader_reachability=failed"
  "${compose[@]}" exec -T api python -c 'print(open("/etc/hosts", encoding="utf-8").read())' || true
  "${compose[@]}" exec -T api python -c 'import socket; print(socket.getaddrinfo("host.docker.internal", 8765))' || true
  "${compose[@]}" logs --tail 30 reader || true
  "${compose[@]}" logs --tail 30 ssrf_proxy || true
  exit 1
fi
echo "reader_reachability=1"
set +e
"$PY" -u "$ROOT/install/installed-proof.py"
proof_ec=$?
set -e
echo "proof_exit=${proof_ec}"
if [[ -f "${state}/installed-run.json" ]]; then
  python3 - "$state" << 'PY'
import json, pathlib, sys
state = pathlib.Path(sys.argv[1])
report = json.loads((state / "installed-run.json").read_text())
names = [row.get("name") for row in report.get("runs", [])]
print("case_union=" + ",".join(name for name in names if name))
print("installed_http=" + str(report.get("installed_http")))
PY
fi
exit "$proof_ec"
