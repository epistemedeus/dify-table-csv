#!/usr/bin/env bash
# Stop the runtime recorded in one owned state directory.
# A foreign directory, a changed inode, a reused pid, or an extra container is left in place.
set -euo pipefail

state="${1:-}"
if [[ -z "$state" || -L "$state" || ! -d "$state" || ! -f "$state/owner.json" || -L "$state/owner.json" ]]; then
  echo "refuse: owned runtime state is missing"
  exit 2
fi

python3 - "$state" << 'PY'
import json, os, re, select, signal, stat, sys
state = sys.argv[1]

def refuse(message):
    print(message)
    raise SystemExit(2)

path = os.path.join(state, "owner.json")
try:
    doc = json.loads(open(path, "r", encoding="utf-8").read())
except (OSError, json.JSONDecodeError):
    refuse("refuse: owned runtime record is unreadable")
token_path = os.path.join(state, "owner.token")
try:
    token_file = open(token_path, "r", encoding="utf-8").read().strip()
except OSError:
    refuse("refuse: owned runtime token is missing")
st = os.lstat(state)
if os.path.islink(state) or not stat.S_ISDIR(st.st_mode):
    refuse("refuse: state directory is not a directory")
if doc.get("schema") != "dify-table-csv.owned-runtime.v1" or doc.get("owned_by") != "dify-table-csv":
    refuse("refuse: state is not an owned dify-table-csv runtime")
if doc.get("token") != token_file or not token_file:
    refuse("refuse: owned runtime token does not match")
if int(doc.get("state_dev")) != st.st_dev or int(doc.get("state_ino")) != st.st_ino:
    refuse("refuse: state directory identity changed")
project = doc.get("project") or ""
if re.fullmatch(r"difytablecsv-[a-f0-9]{8}", project) is None:
    refuse("refuse: project name is not an owned dify-table-csv runtime")
open(os.path.join(state, "stop.project"), "w").write(project + "\n")
containers = doc.get("container_ids") or []
networks = doc.get("network_ids") or []
if not isinstance(containers, list) or not isinstance(networks, list):
    refuse("refuse: owned runtime record is unreadable")
if any(not isinstance(item, str) or re.fullmatch(r"[a-f0-9]{64}", item) is None for item in containers + networks):
    refuse("refuse: owned runtime record is unreadable")
open(os.path.join(state, "stop.containers"), "w").write("\n".join(containers) + ("\n" if containers else ""))
open(os.path.join(state, "stop.networks"), "w").write("\n".join(networks) + ("\n" if networks else ""))

def starttime(pid):
    try:
        raw = open("/proc/%s/stat" % pid, "r", encoding="utf-8").read()
    except OSError:
        return None
    mark = raw.rfind(")")
    if mark < 0:
        return None
    fields = raw[mark + 2:].split()
    if len(fields) < 20:
        return None
    return fields[19]

def cmdline(pid):
    try:
        raw = open("/proc/%s/cmdline" % pid, "rb").read()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace")

def has_token(pid, token):
    try:
        raw = open("/proc/%s/environ" % pid, "rb").read()
    except OSError:
        return False
    needle = ("DIFY_TABLE_OWNER_TOKEN=%s" % token).encode("utf-8")
    return needle in raw.split(b"\0")

pid_path = os.path.join(state, "reader.pid")
recorded = str(doc.get("reader_starttime") or "")
boot = open("/proc/sys/kernel/random/boot_id", encoding="utf-8").read().strip()
if os.path.isfile(pid_path) and not os.path.islink(pid_path):
    pid = open(pid_path, "r", encoding="utf-8").read().strip()
    if pid.isdigit() and int(pid) > 1 and int(pid) != os.getpid():
        # Open the process handle before reading identity. A recycled numeric
        # PID must never redirect the eventual signal to its replacement.
        pidfd = None
        handle_error = False
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            try:
                pidfd = os.pidfd_open(int(pid), 0)
            except ProcessLookupError:
                pass
            except OSError:
                handle_error = True
        else:
            handle_error = True
        try:
            current = starttime(pid)
            command = cmdline(pid)
            exact_identity = (
                str(doc.get("reader_pid")) == pid
                and doc.get("reader_boot_id") == boot
                and recorded and current == recorded
            )
            owned_reader = "reader_boundary.mjs" in command and (
                exact_identity or has_token(pid, token_file)
            )
            if current is None:
                pass
            elif owned_reader:
                if handle_error or pidfd is None:
                    refuse("refuse: safe reader stop is unavailable")
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    refuse("refuse: owned reader signal failed")
                poller = select.poll()
                poller.register(pidfd, select.POLLIN)
                if not poller.poll(5000):
                    refuse("refuse: owned reader did not stop")
                print("reader_stopped=1")
            else:
                print("reader_pid_reused=1")
        finally:
            if pidfd is not None:
                os.close(pidfd)
print("ownership_ok=1")
PY

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  if [[ -s "$state/stop.containers" || -s "$state/stop.networks" ]]; then
    echo "refuse: docker daemon is not reachable"
    exit 2
  fi
  DOCKER=()
fi

project="$(tr -d '[:space:]' < "$state/stop.project")"
if [[ ! "$project" =~ ^difytablecsv-[a-f0-9]{8}$ ]]; then
  echo "refuse: project name is not an owned dify-table-csv runtime"
  exit 2
fi

if [[ ${#DOCKER[@]} -gt 0 ]]; then
  # An empty recorded list is still a record. A live project container that
  # is not in it belongs to someone else and must not be stopped.
  mapfile -t recorded < "$state/stop.containers"
  if ! live_output="$("${DOCKER[@]}" ps -aq --no-trunc --filter "label=com.docker.compose.project=${project}")"; then
    echo "stop_refused=container_inventory_failed"
    exit 2
  fi
  mapfile -t live <<< "$live_output"
  for id in "${live[@]}"; do
    [[ -z "$id" ]] && continue
    known=0
    for want in "${recorded[@]}"; do
      if [[ "$id" == "$want" ]]; then
        known=1
      fi
    done
    if [[ "$known" != "1" ]]; then
      echo "stop_refused=foreign_container"
      exit 2
    fi
  done
fi

# Distinguish an absent object from an unavailable daemon or denied inspection.
# Do not expose raw Docker error output, which can contain transport details.
inspect_owned() {
  local kind="$1" id="$2" format="$3" error
  if INSPECT_VALUE="$("${DOCKER[@]}" inspect --type "$kind" --format "$format" "$id" 2>"$state/stop.inspect-error")"; then
    return 0
  fi
  error="$(cat "$state/stop.inspect-error")"
  case "$error" in
    "Error: No such object: $id"|"Error: No such container: $id"|"Error response from daemon: No such container: $id"|"Error: No such network: $id"|"Error response from daemon: network $id not found")
      return 3 ;;
    *)
      echo "stop_refused=${kind}_inspect_failed"
      return 2 ;;
  esac
}
require_absent() {
  local kind="$1" id="$2" format="$3" ec
  if inspect_owned "$kind" "$id" "$format"; then
    echo "stop_refused=${kind}_still_present"
    return 2
  else
    ec=$?
    [[ "$ec" == "3" ]] || return 2
  fi
}

if [[ ${#DOCKER[@]} -gt 0 ]]; then
  for id in "${recorded[@]}"; do
    [[ -z "$id" ]] && continue
    if inspect_owned container "$id" '{{.State.Running}}'; then
      if [[ "$INSPECT_VALUE" != "true" && "$INSPECT_VALUE" != "false" ]]; then
        echo "stop_refused=container_inspect_invalid"
        exit 2
      fi
      if [[ "$INSPECT_VALUE" == "true" ]]; then
        if ! "${DOCKER[@]}" stop "$id" >/dev/null 2>&1; then
          echo "stop_refused=container_stop_failed"
          exit 2
        fi
      fi
      if ! "${DOCKER[@]}" rm "$id" >/dev/null 2>&1; then
        echo "stop_refused=container_remove_failed"
        exit 2
      fi
      require_absent container "$id" '{{.State.Running}}' || exit 2
    else
      ec=$?
      [[ "$ec" == "3" ]] || exit 2
    fi
  done
fi

if [[ ${#DOCKER[@]} -gt 0 && -s "$state/stop.networks" ]]; then
  while IFS= read -r net; do
    [[ -z "$net" ]] && continue
    if inspect_owned network "$net" '{{len .Containers}}'; then
      if [[ "$INSPECT_VALUE" != "0" ]]; then
        echo "stop_refused=network_in_use"
        exit 2
      fi
      if ! "${DOCKER[@]}" network rm "$net" >/dev/null 2>&1; then
        echo "stop_refused=network_remove_failed"
        exit 2
      fi
      require_absent network "$net" '{{len .Containers}}' || exit 2
    else
      ec=$?
      [[ "$ec" == "3" ]] || exit 2
    fi
  done < "$state/stop.networks"
fi

rm -f "$state/stop.project" "$state/stop.containers" "$state/stop.networks" "$state/stop.inspect-error"
echo "stack_stopped=1"
echo "owned_project=${project}"
