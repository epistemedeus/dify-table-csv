#!/usr/bin/env python3
"""Import, export, reimport, and run the pinned workflow on local Dify.

Without DIFY_CONSOLE_ORIGIN the script records installed_http=not_run and
exits 2. It does not call a public Dify host.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DSL = ROOT / "workflow" / "reader-table-csv.dsl.yml"
MERCHANT = "a1dc09bb076b823fb3492ea79fa10f8602ed01a5"
FIXTURE_URL = (
    "https://raw.githubusercontent.com/epistemedeus/x402-url-extractor/"
    + MERCHANT
    + "/integrations/openserv-reader/fixtures.mjs"
)
SPAN_URL = (
    "https://raw.githubusercontent.com/epistemedeus/x402-url-extractor/"
    + MERCHANT
    + "/integrations/olas-reader/fixtures/olas-reader-runtime.html"
)
FIELDS = ["Engine", "Transpile", "Reconcile"]
NOTE_URL = (
    "https://raw.githubusercontent.com/epistemedeus/dify-table-csv/"
    "a31b79541838c3d0023ee1af6150fae8a7e1cb8d/samples/item-note.html"
)
NOTE_FIELDS = ["Item", "Note"]
FORBIDDEN = {"llm", "agent", "knowledge-retrieval", "knowledge-index", "question-classifier", "parameter-extractor", "tool"}
ALLOWED = {"start", "if-else", "http-request", "code", "end"}


def fail(message, body=""):
    print(message)
    if body:
        text = str(body)
        lowered = text.lower()
        if any(needle in lowered for needle in ("password", "access_token", "secret_key", "api_key")):
            text = "body omitted"
        print(text[:2000])
    raise SystemExit(1)


def load_admin(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def stable_provenance(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        value.pop("fetched_at", None)
    return value


class Console:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = None

    def request(self, method, path, payload=None, token=None, timeout=180):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        auth = token if token is not None else self.token
        if auth:
            headers["Authorization"] = "Bearer " + auth
        for cookie in self.jar:
            if cookie.name == "csrf_token" and cookie.value:
                headers["X-CSRF-Token"] = cookie.value
        req = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed

    def import_yaml(self, yaml_text):
        code, imported = self.request(
            "POST",
            "/console/api/apps/imports",
            {"mode": "yaml-content", "yaml_content": yaml_text},
        )
        if code not in (200, 201) or not isinstance(imported, dict):
            fail("import failed", imported)
        if imported.get("status") == "pending":
            code, imported = self.request("POST", "/console/api/apps/imports/%s/confirm" % imported["id"])
            if code not in (200, 201) or not isinstance(imported, dict):
                fail("import confirm failed", imported)
        if imported.get("status") not in ("completed", "completed-with-warnings"):
            fail("import status %s" % imported.get("status"), json.dumps(imported)[:2000])
        if not imported.get("app_id"):
            fail("import returned no app_id", json.dumps(imported)[:2000])
        return imported


def health_count(base):
    with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))["requestCount"]


def wait_health(base, present, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            health_count(base)
            if present:
                return True
        except (urllib.error.URLError, TimeoutError, OSError):
            if not present:
                return True
        time.sleep(0.4)
    return False


def outputs_of(run_body):
    data = run_body.get("data") if isinstance(run_body, dict) and isinstance(run_body.get("data"), dict) else {}
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else None
    return data, outputs


def billing_ok(data):
    tokens = data.get("total_tokens")
    return tokens in (0, "0", 0.0)


def summarize_run(name, run_code, run_body, before_count, after_count):
    data, outputs = outputs_of(run_body if isinstance(run_body, dict) else {})
    return {
        "name": name,
        "http_status": run_code,
        "workflow_status": data.get("status") if isinstance(data, dict) else None,
        "error": (data.get("error") if isinstance(data, dict) else None),
        "total_tokens": data.get("total_tokens") if isinstance(data, dict) else None,
        "total_steps": data.get("total_steps") if isinstance(data, dict) else None,
        "elapsed_time": data.get("elapsed_time") if isinstance(data, dict) else None,
        "output_status": None if not isinstance(outputs, dict) else outputs.get("status"),
        "csv": None if not isinstance(outputs, dict) else outputs.get("csv"),
        "retryable": None if not isinstance(outputs, dict) else outputs.get("retryable"),
        "provenance": None if not isinstance(outputs, dict) else outputs.get("provenance"),
        "handoff": None if not isinstance(outputs, dict) else outputs.get("handoff"),
        "reader_request_delta": None if before_count is None else after_count - before_count,
    }


def main():
    origin = os.environ.get("DIFY_CONSOLE_ORIGIN", "").rstrip("/")
    if not origin:
        print("installed_http=not_run")
        print("DIFY_CONSOLE_ORIGIN is unset. This does not start Dify and does not call a public host.")
        raise SystemExit(2)
    host = urlparse(origin).hostname
    if host not in {"127.0.0.1", "localhost"}:
        print("installed_http=not_run")
        print("refusing non-local console origin")
        raise SystemExit(2)
    admin_file = os.environ.get("DIFY_LOCAL_ADMIN_FILE", "")
    if not admin_file:
        fail("DIFY_LOCAL_ADMIN_FILE is required with a local console origin")
    admin = load_admin(admin_file)
    reader_base = os.environ.get("READER_BASE", "http://127.0.0.1:8765")
    from direct_client import run_job

    console = Console(origin)
    status, body = console.request("GET", "/console/api/setup")
    if status != 200 or not isinstance(body, dict):
        fail("setup status failed", body)
    init_status, init_body = console.request("GET", "/console/api/init")
    if init_status == 200 and isinstance(init_body, dict) and init_body.get("status") == "not_started":
        code, init_post = console.request("POST", "/console/api/init", {"password": admin["init_password"]})
        if code not in (200, 201):
            fail("init validate failed", init_post)
    if body.get("step") == "not_started":
        code, setup_body = console.request(
            "POST",
            "/console/api/setup",
            {
                "email": admin["email"],
                "name": admin["name"],
                "password": admin["password"],
                "language": "en-US",
            },
        )
        if code not in (200, 201):
            fail("setup failed", setup_body)
    import base64

    login_code, login_body = console.request(
        "POST",
        "/console/api/login",
        {
            "email": admin["email"],
            "password": base64.b64encode(admin["password"].encode("utf-8")).decode("ascii"),
            "language": "en-US",
            "remember_me": True,
        },
    )
    if login_code not in (200, 201) or not isinstance(login_body, dict):
        fail("login failed", login_body)
    data = login_body.get("data") if isinstance(login_body.get("data"), dict) else {}
    token = data.get("access_token") or login_body.get("access_token")
    if not token:
        # Dify 1.18 returns result=success with data null and puts the token in the access_token cookie.
        for cookie in console.jar:
            if cookie.name == "access_token" and cookie.value:
                token = cookie.value.strip().strip('"')
                break
    if not token:
        fail("login returned no access token", {k: login_body.get(k) for k in login_body if k != "data"})
    console.token = token

    first = console.import_yaml(DSL.read_text())
    # The console default export is an app package. format=yaml is the DSL export.
    export_code, exported = console.request(
        "GET", "/console/api/apps/%s/export?include_secret=false&format=yaml" % first["app_id"]
    )
    if export_code != 200 or not isinstance(exported, dict) or not isinstance(exported.get("data"), str):
        fail("export failed", exported)
    export_text = exported["data"]
    try:
        import yaml
    except ImportError:
        fail("pyyaml is required to read the engine export")
    exported_doc = yaml.safe_load(export_text)
    kinds = [node["data"]["type"] for node in exported_doc["workflow"]["graph"]["nodes"]]
    if FORBIDDEN.intersection(kinds) or not set(kinds).issubset(ALLOWED):
        fail("export graph is not the deterministic reader workflow", kinds)
    http_nodes = [node["data"] for node in exported_doc["workflow"]["graph"]["nodes"] if node["data"]["type"] == "http-request"]
    if not http_nodes or http_nodes[0].get("retry_config", {}).get("retry_enabled") not in (False, None):
        fail("exported http node enabled retry", http_nodes[0].get("retry_config") if http_nodes else None)
    (ROOT / "evidence").mkdir(exist_ok=True)
    (ROOT / "evidence" / "export.dsl.yml").write_text(export_text)
    second = console.import_yaml(export_text)
    publish_code, published = console.request(
        "POST",
        "/console/api/apps/%s/workflows/publish" % second["app_id"],
        {"marked_name": "h64314", "marked_comment": "local proof"},
    )
    if publish_code not in (200, 201):
        publish_code, published = console.request(
            "POST",
            "/console/api/apps/%s/workflows/publish" % second["app_id"],
            {},
        )
    if publish_code not in (200, 201):
        fail("publish failed", published)
    key_code, key_body = console.request("POST", "/console/api/apps/%s/api-keys" % second["app_id"], {})
    if key_code not in (200, 201) or not isinstance(key_body, dict) or not key_body.get("token"):
        fail("api key create failed", key_body)
    app_token = key_body["token"]

    def run(inputs):
        return console.request(
            "POST",
            "/v1/workflows/run",
            {"inputs": inputs, "response_mode": "blocking", "user": "h64314-local-test"},
            token=app_token,
            timeout=180,
        )

    def once(name, inputs, direct):
        before = health_count(reader_base)
        code, body = run(inputs)
        after = health_count(reader_base)
        row = summarize_run(name, code, body, before, after)
        row["direct_status"] = direct["status"]
        row["direct_csv"] = direct["csv"]
        row["csv_equal"] = isinstance(row["csv"], str) and row["csv"] == direct["csv"]
        row["status_equal"] = row["output_status"] == direct["status"]
        row["provenance_equal"] = stable_provenance(row["provenance"]) == stable_provenance(direct["provenance"])
        row["handoff_equal"] = row["handoff"] == direct["handoff"]
        row["billing_zero"] = billing_ok(outputs_of(body)[0])
        return row

    runs = []
    page1 = {
        "document_url": FIXTURE_URL,
        "fields_json": json.dumps(FIELDS),
        "markdown_limit": 40000,
        "page": 1,
    }
    # The worker may still be attaching to the broker on the first call.
    # Count reader requests around the attempt that actually finishes.
    ready = None
    before_success = None
    after_success = None
    for attempt in range(5):
        before_try = health_count(reader_base)
        ready_code, ready_body = run(page1)
        after_try = health_count(reader_base)
        data, _outputs = outputs_of(ready_body if isinstance(ready_body, dict) else {})
        if ready_code == 200 and isinstance(data, dict) and data.get("status") in {"succeeded", "failed", "stopped"}:
            ready = (ready_code, ready_body)
            before_success = before_try
            after_success = after_try
            break
        print("workflow_not_ready attempt=%s http=%s" % (attempt + 1, ready_code))
        time.sleep(3)
    if ready is None:
        fail("workflow run did not become ready", ready_body)
    first_row = summarize_run("page1_fixture_ready", ready[0], ready[1], before_success, after_success)
    direct_page1 = run_job(reader_base, FIXTURE_URL, FIELDS, 40000, 1)
    _data, first_outputs = outputs_of(ready[1] if isinstance(ready[1], dict) else {})
    if isinstance(first_outputs, dict):
        first_row["csv"] = first_outputs.get("csv")
        first_row["output_status"] = first_outputs.get("status")
        first_row["provenance"] = first_outputs.get("provenance")
        first_row["handoff"] = first_outputs.get("handoff")
        first_row["retryable"] = first_outputs.get("retryable")
    first_row["direct_status"] = direct_page1["status"]
    first_row["direct_csv"] = direct_page1["csv"]
    first_row["csv_equal"] = first_row["csv"] == direct_page1["csv"]
    first_row["status_equal"] = first_row["output_status"] == direct_page1["status"]
    first_row["provenance_equal"] = stable_provenance(first_row["provenance"]) == stable_provenance(direct_page1["provenance"])
    first_row["handoff_equal"] = first_row["handoff"] == direct_page1["handoff"]
    first_row["billing_zero"] = billing_ok(outputs_of(ready[1])[0])
    runs.append(first_row)

    direct_note = run_job(reader_base, NOTE_URL, NOTE_FIELDS, 40000, 1)
    runs.append(
        once(
            "item_note_public",
            {
                "document_url": NOTE_URL,
                "fields_json": json.dumps(NOTE_FIELDS),
                "markdown_limit": 40000,
                "page": 1,
            },
            direct_note,
        )
    )

    direct_page2 = run_job(reader_base, FIXTURE_URL, FIELDS, 40000, 2)
    runs.append(once("page2_gate", {**page1, "page": 2}, direct_page2))
    direct_span = run_job(reader_base, SPAN_URL, ["Engine", "Reconcile"], 40000, 1)
    runs.append(
        once(
            "combined_spans",
            {
                "document_url": SPAN_URL,
                "fields_json": json.dumps(["Engine", "Reconcile"]),
                "markdown_limit": 40000,
                "page": 1,
            },
            direct_span,
        )
    )
    direct_dns = run_job(reader_base, "https://h64314-no-such-host.invalid/table", FIELDS, 40000, 1)
    runs.append(
        once(
            "dns_transport",
            {
                "document_url": "https://h64314-no-such-host.invalid/table",
                "fields_json": json.dumps(FIELDS),
                "markdown_limit": 40000,
                "page": 1,
            },
            direct_dns,
        )
    )

    pid_file = os.environ.get("READER_PID_FILE", "")
    lost = {"name": "reader_down"}
    recovered = {"name": "reader_recovered"}
    if pid_file and Path(pid_file).is_file():
        pid = int(Path(pid_file).read_text().strip())
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if not wait_health(reader_base, present=False):
            fail("reader did not stop")
        lost_code, lost_body = run(page1)
        lost = summarize_run("reader_down", lost_code, lost_body, None, None)
        lost["succeeded_with_csv"] = lost.get("output_status") == "ok" and bool(lost.get("csv"))
        env = os.environ.copy()
        env["H64314_ADMIT_DIFY"] = "1"
        restart_log = open(Path(pid_file).with_name("reader-boundary-restart.log"), "ab")
        proc = subprocess.Popen(
            ["node", str(ROOT / "src" / "reader_boundary.mjs")],
            env=env,
            stdout=restart_log,
            stderr=subprocess.STDOUT,
        )
        restart_log.close()
        Path(pid_file).write_text(str(proc.pid) + "\n")
        if not wait_health(reader_base, present=True):
            fail("reader did not restart", proc.pid)
        direct_recovered = run_job(reader_base, FIXTURE_URL, FIELDS, 40000, 1)
        recovered = once("reader_recovered", page1, direct_recovered)
        runs.extend([lost, recovered])
    else:
        runs.append({"name": "reader_down", "skipped": True})

    report = {
        "installed_http": "failed",
        "import_app_id": first["app_id"],
        "import_status": first.get("status"),
        "reimport_app_id": second["app_id"],
        "reimport_status": second.get("status"),
        "export_format": "yaml",
        "export_include_secret": False,
        "exported_node_types": kinds,
        "publish_http": publish_code,
        "runs": runs,
    }
    required = {
        "page1_fixture_ready": {
            "status_equal": True,
            "csv_equal": True,
            "provenance_equal": True,
            "handoff_equal": True,
            "billing_zero": True,
            "workflow_status": "succeeded",
            "reader_request_delta": 1,
        },
        "item_note_public": {
            "status_equal": True,
            "csv_equal": True,
            "provenance_equal": True,
            "handoff_equal": True,
            "billing_zero": True,
            "workflow_status": "succeeded",
            "output_status": "ok",
            "reader_request_delta": 1,
        },
        "page2_gate": {"status_equal": True, "csv_equal": True, "provenance_equal": True, "handoff_equal": True, "reader_request_delta": 0, "billing_zero": True},
        "combined_spans": {"status_equal": True, "csv_equal": True, "provenance_equal": True, "handoff_equal": True, "billing_zero": True},
        "dns_transport": {"status_equal": True, "csv_equal": True, "provenance_equal": True, "handoff_equal": True, "billing_zero": True},
        "reader_recovered": {"status_equal": True, "csv_equal": True, "provenance_equal": True, "handoff_equal": True, "billing_zero": True, "workflow_status": "succeeded"},
    }
    problems = []
    by_name = {row["name"]: row for row in runs}
    for name, expect in required.items():
        row = by_name.get(name)
        if row is None:
            problems.append(name + " missing")
            continue
        for key, value in expect.items():
            if row.get(key) != value:
                problems.append("%s %s=%s expected %s" % (name, key, row.get(key), value))
    down = by_name.get("reader_down")
    if down and not down.get("skipped") and down.get("succeeded_with_csv"):
        problems.append("reader_down produced a CSV while the reader was stopped")
    if down and not down.get("skipped") and down.get("workflow_status") == "succeeded" and down.get("output_status") == "ok":
        problems.append("reader_down succeeded")
    # Observed Dify 1.18 classification: a stopped reader is HTTP 503, the
    # workflow run succeeds, and the checker returns malformed with no CSV.
    if down and not down.get("skipped"):
        if down.get("output_status") != "malformed":
            problems.append("reader_down classification %s expected malformed" % down.get("output_status"))
        if down.get("csv") not in ("", None):
            problems.append("reader_down returned a CSV")
        if isinstance(down.get("csv"), str) and "Engine,Transpile,Reconcile" in down.get("csv"):
            problems.append("reader_down returned the fixture CSV")
        if down.get("retryable") not in ("false", False):
            problems.append("reader_down retryable %s expected false" % down.get("retryable"))
        if down.get("workflow_status") != "succeeded":
            problems.append("reader_down workflow %s expected succeeded" % down.get("workflow_status"))
    report["problems"] = problems
    report["installed_http"] = "passed" if not problems else "failed"
    destination = Path(os.environ.get("H64314_EVIDENCE", ROOT / "evidence" / "installed-run.json"))
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("installed_http=%s" % report["installed_http"])
    print("import_app_id=%s" % first["app_id"])
    print("reimport_app_id=%s" % second["app_id"])
    print("export_nodes=%s" % ",".join(kinds))
    frame_runs = []
    for row in runs:
        csv_text = row.get("csv") if isinstance(row.get("csv"), str) else ""
        print(
            "run %s workflow=%s output=%s tokens=%s delta=%s"
            % (row.get("name"), row.get("workflow_status"), row.get("output_status"), row.get("total_tokens"), row.get("reader_request_delta"))
        )
        frame_runs.append({
            "name": row.get("name"),
            "http_status": row.get("http_status"),
            "workflow_status": row.get("workflow_status"),
            "output_status": row.get("output_status"),
            "total_tokens": row.get("total_tokens"),
            "total_steps": row.get("total_steps"),
            "reader_request_delta": row.get("reader_request_delta"),
            "retryable": row.get("retryable"),
            "csv_bytes": len(csv_text.encode("utf-8")),
            "csv_sha256": hashlib.sha256(csv_text.encode("utf-8")).hexdigest(),
            "status_equal": row.get("status_equal"),
            "csv_equal": row.get("csv_equal"),
            "provenance_equal": row.get("provenance_equal"),
            "handoff_equal": row.get("handoff_equal"),
            "billing_zero": row.get("billing_zero"),
            "skipped": row.get("skipped", False),
        })
    frame = {
        "schema": "h80300.dify-installed-frame.v1",
        "installed_http": report["installed_http"],
        "export_format": report["export_format"],
        "export_include_secret": report["export_include_secret"],
        "exported_node_types": report["exported_node_types"],
        "import_status": report["import_status"],
        "reimport_status": report["reimport_status"],
        "publish_http": report["publish_http"],
        "problems": problems,
        "runs": frame_runs,
    }
    print("AE14_EVIDENCE_FRAME_BEGIN")
    print(json.dumps(frame, sort_keys=True))
    print("AE14_EVIDENCE_FRAME_END")
    if problems:
        fail("installed comparison failed", "\n".join(problems))
    print("csv_status=%s" % by_name["page1_fixture_ready"].get("output_status"))


if __name__ == "__main__":
    main()
