"""Stage1 checks. The Dify engine is not mocked and is not started."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

import table_csv  # noqa: E402
from build_dsl import FORBIDDEN, READER_URL, build_document, check_code, refuse_code, shared_source  # noqa: E402
from direct_client import get_health, post_read, run_job  # noqa: E402

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
W3C_URL = "https://www.w3.org/WAI/tutorials/tables/one-header/"
FIELDS = ["Engine", "Transpile", "Reconcile"]
CORE = os.environ.get("READER_CORE", str(ROOT / "reader"))


def record(markdown, truncated=False, source_ok=True, error=None, status=200):
    return {
        "ok": True,
        "requestedUrl": "https://example.com/doc",
        "finalUrl": "https://example.com/doc",
        "url": "https://example.com/doc",
        "status": status,
        "sourceOk": source_ok,
        "error": error,
        "title": "Example",
        "markdown": markdown,
        "wordCount": 3,
        "truncated": truncated,
        "capture": {"method": "http-get-no-javascript"},
        "fetchedAt": "2026-10-29T00:00:00Z",
    }


def packed(markdown, fields=None, truncated=False, limit=40000, page=1, status_code=200, body=None):
    payload = body if body is not None else json.dumps(record(markdown, truncated=truncated))
    chosen = FIELDS if fields is None else fields
    return table_csv.main(
        "https://example.com/doc",
        json.dumps(chosen),
        limit,
        page,
        payload,
        status_code,
    )


class TestChecker(unittest.TestCase):
    def test_rectangular_csv_and_roundtrip(self):
        markdown = (
            "<table><tr><th>Engine</th><th>Transpile</th><th>Reconcile</th></tr>"
            "<tr><td>Alpha</td><td></td><td>Yes</td></tr>"
            "<tr><td>Beta</td><td>No</td><td></td></tr></table>"
        )
        result = packed(markdown)
        self.assertEqual(result["status"], "ok")
        rows = list(csv.reader(io.StringIO(result["csv"])))
        self.assertEqual(rows, [["Engine", "Transpile", "Reconcile"], ["Alpha", "", "Yes"], ["Beta", "No", ""]])
        provenance = json.loads(result["provenance"])
        self.assertEqual(provenance["completeness"], "reader_record")
        self.assertEqual(provenance["row_count"], 2)
        self.assertFalse(provenance["reader_truncated"])
        self.assertEqual(provenance["reader_merchant_commit"], table_csv.READER_MERCHANT_COMMIT)
        self.assertIn("CSV data rows: 2.", result["handoff"])
        self.assertIn("Document: https://example.com/doc", result["handoff"])
        self.assertNotIn(
            "This handoff is local experiment output. It is not partner acceptance, a customer delivery, or revenue.",
            result["handoff"],
        )

    def test_truncated_selection_does_not_claim_full_document(self):
        markdown = "<table><tr><th>Engine</th><th>Transpile</th><th>Reconcile</th></tr><tr><td>Alpha</td><td></td><td>Yes</td></tr></table>"
        result = packed(markdown, truncated=True)
        provenance = json.loads(result["provenance"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provenance["completeness"], "selected_table")
        self.assertIn("not a full-document extract", result["handoff"])

    def test_field_order_follows_the_request(self):
        markdown = "<table><tr><th>Engine</th><th>Reconcile</th></tr><tr><td>Alpha</td><td>Yes</td></tr></table>"
        result = packed(markdown, fields=["Reconcile", "Engine"])
        rows = list(csv.reader(io.StringIO(result["csv"])))
        self.assertEqual(rows, [["Reconcile", "Engine"], ["Yes", "Alpha"]])

    def test_combined_spans_are_not_flattened(self):
        markdown = '<table><tr><th rowspan="2">Engine</th><th>Reconcile</th></tr><tr><td>Alpha</td><td>Yes</td></tr></table>'
        result = packed(markdown, fields=["Engine", "Reconcile"])
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["csv"], "")
        provenance = json.loads(result["provenance"])
        self.assertIn("combined spans", provenance["unsupported_tables"])
        self.assertNotIn("Alpha", result["csv"])
        self.assertIn("does not guess a rectangular grid", result["handoff"])

    def test_source_refused_is_a_checker_result(self):
        body = json.dumps(record("no table", source_ok=False, error="http_404", status=404))
        result = packed("", body=body)
        self.assertEqual(result["status"], "source_refused")
        self.assertEqual(result["csv"], "")
        self.assertIn("source_refused", result["handoff"])

    def test_http_503_is_malformed_and_returns_no_fixture_csv(self):
        result = packed("<table><tr><th>Engine</th></tr><tr><td>Alpha</td></tr></table>", status_code=503, body="unavailable")
        self.assertEqual(result["status"], "malformed")
        self.assertEqual(result["csv"], "")
        self.assertEqual(result["retryable"], "false")
        self.assertNotIn("Alpha", result["csv"])
        provenance = json.loads(result["provenance"])
        self.assertFalse(provenance["retryable"])

    def test_malformed_json_and_bounds(self):
        self.assertEqual(packed("", body="not-json")["status"], "malformed")
        self.assertEqual(packed("<table></table>", fields=[])["status"], "unsupported")
        self.assertEqual(packed("<table></table>", fields=["A"] * 33)["status"], "unsupported")
        self.assertEqual(packed("<table></table>", fields=["A", "A"])["status"], "unsupported")
        self.assertEqual(packed("<table></table>", fields=["bad\nname"])["status"], "unsupported")
        self.assertEqual(packed("<table></table>", limit=0)["status"], "unsupported")
        self.assertEqual(packed("<table></table>", limit=40001)["status"], "unsupported")
        page = table_csv.main("https://example.com/doc", json.dumps(["A"]), 100, 2, "", 0)
        self.assertEqual(page["status"], "unsupported")
        self.assertIn("no page cursor", page["handoff"])

    def test_missing_field_and_no_table(self):
        markdown = "<table><tr><th>Engine</th></tr><tr><td>Alpha</td></tr></table>"
        self.assertEqual(packed(markdown, fields=["Missing"])["status"], "unsupported")
        self.assertEqual(packed("Just a paragraph.", fields=["Title"])["status"], "unsupported")

    def test_same_body_matches_direct_checker(self):
        markdown = "<table><tr><th>Engine</th><th>Transpile</th><th>Reconcile</th></tr><tr><td>Alpha</td><td></td><td>Yes</td></tr></table>"
        body = json.dumps(record(markdown))
        graph = table_csv.main(FIXTURE_URL, json.dumps(FIELDS), 40000, 1, body, 200)
        direct = table_csv._pack(
            table_csv.evaluate(body, 200, FIXTURE_URL, json.dumps(FIELDS), 40000, 1, explicit_retry=0)
        )
        self.assertEqual(graph, direct)

    def test_embedded_code_matches_module(self):
        markdown = "<table><tr><th>Engine</th><th>Transpile</th><th>Reconcile</th></tr><tr><td>Alpha</td><td></td><td>Yes</td></tr></table>"
        body = json.dumps(record(markdown, truncated=True))
        namespace = {}
        exec(check_code(), namespace)
        embedded = namespace["main"](FIXTURE_URL, json.dumps(FIELDS), 40000, 1, body, 200)
        native = table_csv.main(FIXTURE_URL, json.dumps(FIELDS), 40000, 1, body, 200)
        self.assertEqual(embedded, native)
        refused = {}
        exec(refuse_code(), refused)
        page = refused["main"](FIXTURE_URL, json.dumps(FIELDS), 40000, 2)
        self.assertEqual(page["status"], "unsupported")
        self.assertIn("explicit_retry=0", check_code())
        self.assertIn(shared_source().strip().splitlines()[0], check_code())


class TestDsl(unittest.TestCase):
    def test_workflow_contract(self):
        document = build_document()
        raw = (ROOT / "workflow" / "reader-table-csv.dsl.yml").read_text()
        self.assertNotIn(
            "This handoff is local experiment output. It is not partner acceptance, a customer delivery, or revenue.",
            raw,
        )
        self.assertEqual(raw.count("def _handoff"), 2)
        self.assertEqual(raw.count("not a full-document extract"), 2)
        self.assertIn("no page cursor", raw)
        self.assertEqual(raw.count("does not guess a rectangular grid"), 2)
        loaded = yaml.safe_load(raw)
        again = yaml.safe_load(yaml.safe_dump(loaded, sort_keys=False, allow_unicode=True, width=4096))
        self.assertEqual(again, loaded)
        self.assertEqual(loaded, document)
        self.assertEqual(loaded["version"], "0.7.0")
        self.assertEqual(loaded["kind"], "app")
        self.assertEqual(loaded["app"]["mode"], "workflow")
        self.assertEqual(loaded["dependencies"], [])
        nodes = loaded["workflow"]["graph"]["nodes"]
        kinds = [node["data"]["type"] for node in nodes]
        self.assertEqual(FORBIDDEN.intersection(kinds), set())
        http = next(node["data"] for node in nodes if node["id"] == "reader_http")
        self.assertEqual(http["url"], READER_URL)
        self.assertFalse(http["retry_config"]["retry_enabled"])
        self.assertEqual(http["retry_config"]["max_retries"], 0)
        self.assertEqual(sorted(item["variable"] for item in next(node["data"] for node in nodes if node["id"] == "check_csv")["variables"]),
                         sorted(["document_url", "fields_json", "markdown_limit", "page", "body", "status_code"]))
        code = next(node["data"]["code"] for node in nodes if node["id"] == "check_csv")
        self.assertIn(shared_source().strip(), code)
        self.assertNotIn("openai", code.lower())
        self.assertNotIn("llm", kinds)

    def test_compatibility_pins_match_checker(self):
        pins = json.loads((ROOT / "COMPATIBILITY.json").read_text())
        self.assertEqual(pins["reader"]["merchant_commit"], table_csv.READER_MERCHANT_COMMIT)
        self.assertEqual(pins["reader"]["core_commit"], table_csv.READER_CORE_COMMIT)
        self.assertEqual(pins["reader"]["extract_sha256"], "e087cdcc6bbb019fa9ea0b8ca2e5f7d9e682dfb2bfd6b205e119330d32a5c6e1")
        self.assertEqual(pins["dify"]["commit"], "8e689e868b63b6dd2b449d0eb568ca5cc7127759")
        self.assertEqual(pins["dify"]["release"], "1.19.0")
        self.assertEqual(pins["dify"]["dsl_version"], "0.7.0")
        self.assertEqual(pins["dify"]["license_sha256"], "232cf91474932d5110ed304e53b6b742a58463857c571fae803fdf2ac36d7bb3")
        self.assertEqual(pins["http_503"], "malformed")
        self.assertFalse(pins["model_node"])
        self.assertIsNone(pins["weaviate_digest"])


class TestAdmission(unittest.TestCase):
    def _run(self, extra):
        env = os.environ.copy()
        for key in (
            "H64314_WEAVIATE_DIGEST",
            "H64314_ADMIT_DIFY",
            "DIFY_TABLE_ADMIT",
            "DIFY_SOURCE",
            "H64314_DIFY_SRC",
            "DIFY_TABLE_PROJECT",
            "DIFY_TABLE_KEEP",
            "DIFY_TABLE_STATE",
        ):
            env.pop(key, None)
        env.update(extra)
        completed = subprocess.run(
            ["bash", str(ROOT / "install" / "cold-install.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        return completed

    def test_clean_install_refuses_without_flag(self):
        completed = self._run({})
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("installed_admission_boundary", completed.stdout)
        self.assertNotIn("8388608", completed.stdout)
        self.assertIn("not_installed", completed.stdout)
        self.assertIn("does not docker pull", completed.stdout)

    def test_clean_install_pin_does_not_encode_the_old_floor(self):
        # The admission flag now starts the real engine. This check stays static.
        script = (ROOT / "install" / "cold-install.sh").read_text()
        pin = (ROOT / "install" / "compose.pin.yml").read_text()
        self.assertNotIn("8388608", script)
        self.assertNotIn("H64314_WEAVIATE_DIGEST", script)
        self.assertIn("export COMPOSE_PROFILES=postgresql", script)
        self.assertIn("collision_refused", script)
        self.assertIn("DIFY_TABLE_ADMIT", script)
        self.assertIn("H64314_ADMIT_DIFY", script)
        self.assertIn("network_gateway_mode=nat", script)
        self.assertIn("refuse: state directory already exists", script)
        self.assertIn("export-tracked-docker.sh", script)
        self.assertNotIn("nat-unprotected", script)
        self.assertNotIn("iptables", script)
        self.assertNotIn("br-*", script)
        self.assertIn('python3 "$ROOT/install/preflight.py"', script)
        self.assertLess(script.index("refuse: docker daemon is not reachable"), script.index('python3 "$ROOT/install/preflight.py"'))
        self.assertLess(script.index('python3 "$ROOT/install/preflight.py"'), script.index("docker_pull_begin"))
        self.assertNotIn('cp -a "${src}/docker"', script)
        self.assertNotIn("rm -rf", script)
        self.assertNotIn("sysctl -w", script)
        self.assertNotIn("--remove-orphans", script)
        self.assertIn("127.0.0.1:5001:5001", pin)
        self.assertIn("!reset", pin)
        self.assertIn("com.docker.network.bridge.gateway_mode_ipv4: nat", pin)
        self.assertIn("host.docker.internal:${DIFY_TABLE_DOCKER0}", pin)
        self.assertNotIn("extra_hosts: !reset", pin)
        self.assertIn('export DIFY_TABLE_DOCKER0="$gateway"', script)
        self.assertIn('python3 "$ROOT/install/verify-compose-hosts.py" "$gateway" < "$rendered"', script)
        host_helper = (ROOT / "install" / "verify-compose-hosts.py").read_text()
        self.assertIn('print("composed_host_docker_internal=" + str(count))', host_helper)
        self.assertNotIn("10.240.80", pin)
        self.assertNotIn("\n  reader:", pin)
        self.assertIn("hosts_file /etc/hosts", script)
        self.assertIn('export READER_BIND="$gateway"', script)
        self.assertIn("refuse: docker0 gateway is unknown, so no single-host SSRF allow was written", script)
        self.assertNotIn("${state}/bin/node", script)
        self.assertNotIn("nat-unprotected", pin)
        self.assertNotIn("\n  weaviate:", pin)
        self.assertNotIn("weaviate@", pin)

    def test_invalid_project_refuses_before_docker(self):
        completed = self._run({"DIFY_TABLE_ADMIT": "1", "DIFY_TABLE_PROJECT": "difytable"})
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("refuse: project name", completed.stdout)
        self.assertNotIn("docker_pull_begin", completed.stdout)
        self.assertNotIn("owned_project=", completed.stdout)

    def test_installed_proof_is_not_run(self):
        env = os.environ.copy()
        env.pop("DIFY_CONSOLE_ORIGIN", None)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "install" / "installed-proof.py")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("installed_http=not_run", completed.stdout)

    def test_boundary_refuses_public_bind_without_admission(self):
        completed = subprocess.run(
            ["node", str(ROOT / "src" / "reader_boundary.mjs")],
            env={"READER_BIND": "0.0.0.0", "PORT": "0", "PATH": os.environ.get("PATH", "")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("non-loopback", completed.stderr)


class TestBoundary(unittest.TestCase):
    proc = None
    port = None
    stderr_lines = None

    @classmethod
    def setUpClass(cls):
        if not Path(CORE, "extract.mjs").is_file() or not Path(CORE, "node_modules").is_dir():
            raise RuntimeError("READER_CORE is not prepared at %s" % CORE)
        cls.stderr_lines = []
        cls.proc = subprocess.Popen(
            ["node", str(ROOT / "src" / "reader_boundary.mjs")],
            env={
                **os.environ,
                "READER_CORE": CORE,
                "PORT": "0",
                "READER_BIND": "127.0.0.1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        thread = threading.Thread(target=cls._drain, args=(cls.proc.stderr,), daemon=True)
        thread.start()
        line = cls.proc.stdout.readline()
        if not line.startswith("PORT "):
            raise RuntimeError("boundary did not listen: %s" % line)
        cls.port = int(line.split()[1])
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def _drain(cls, pipe):
        for line in pipe:
            cls.stderr_lines.append(line)

    @classmethod
    def tearDownClass(cls):
        if cls.proc is not None and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    def health_count(self):
        return get_health(self.base)["requestCount"]

    def test_success_unicode_and_same_checker_bytes(self):
        started = time.perf_counter()
        status, body = post_read(self.base, FIXTURE_URL, 40000)
        elapsed = time.perf_counter() - started
        self.assertEqual(status, 200)
        graph = table_csv.main(FIXTURE_URL, json.dumps(FIELDS), 40000, 1, body, status)
        direct = table_csv._pack(table_csv.evaluate(body, status, FIXTURE_URL, json.dumps(FIELDS), 40000, 1))
        self.assertEqual(graph, direct)
        self.assertEqual(graph["status"], "ok")
        rows = list(csv.reader(io.StringIO(graph["csv"])))
        self.assertEqual(rows[0], FIELDS)
        self.assertEqual(rows[1], ["Alpha", "", "Yes"])
        self.assertEqual(rows[2], ["Beta", "No", ""])
        provenance = json.loads(graph["provenance"])
        self.assertTrue(provenance["reader_truncated"])
        self.assertEqual(provenance["completeness"], "selected_table")
        self.assertIn(FIXTURE_URL, graph["handoff"])
        self.assertIn("not a full-document extract", graph["handoff"])
        unicode_result = table_csv.main(FIXTURE_URL, json.dumps(["Meaning", "Value"]), 40000, 1, body, status)
        unicode_rows = list(csv.reader(io.StringIO(unicode_result["csv"])))
        self.assertEqual(unicode_result["status"], "ok")
        self.assertEqual(unicode_rows[0], ["Meaning", "Value"])
        self.assertEqual(unicode_rows[1][0], "中文")
        self.assertIn("€", unicode_rows[1][1])
        self.assertIn("😀", unicode_rows[1][1])
        self.assertEqual(unicode_rows[2], ["Guide", ""])
        self.__class__.fixture_elapsed = elapsed
        self.__class__.fixture_csv_sha = __import__("hashlib").sha256(graph["csv"].encode()).hexdigest()

    def test_limit_clip_does_not_invent_a_table(self):
        before = self.health_count()
        result = run_job(self.base, FIXTURE_URL, FIELDS, 100, 1)
        self.assertEqual(self.health_count() - before, 1)
        self.assertEqual(result["status"], "unsupported")
        self.assertNotIn("Alpha", result["csv"])
        provenance = json.loads(result["provenance"])
        self.assertTrue(provenance["reader_truncated"])

    def test_span_fixture_and_pages_without_tables(self):
        before = self.health_count()
        spans = run_job(self.base, SPAN_URL, FIELDS, 40000, 1)
        self.assertEqual(spans["status"], "unsupported")
        self.assertEqual(spans["csv"], "")
        self.assertIn("combined spans", json.loads(spans["provenance"])["unsupported_tables"])
        empty = run_job(self.base, "https://example.com", ["Title"], 40000, 1)
        self.assertEqual(empty["status"], "unsupported")
        self.assertIn("no HTML table", empty["handoff"])
        w3c = run_job(self.base, W3C_URL, ["Header"], 40000, 1)
        record_body_status, record_body = post_read(self.base, W3C_URL, 40000)
        payload = json.loads(record_body)
        dropped = "<table" not in payload["markdown"] and "Table omitted:" not in payload["markdown"]
        if dropped:
            self.assertEqual(w3c["status"], "unsupported")
            self.assertEqual(w3c["csv"], "")
        self.__class__.w3c_dropped_tables = dropped
        self.__class__.w3c_status = record_body_status
        self.assertGreaterEqual(self.health_count() - before, 3)

    def test_page_gate_does_not_call_reader(self):
        before = self.health_count()
        result = run_job(self.base, FIXTURE_URL, FIELDS, 40000, 2)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(self.health_count(), before)
        self.assertIn("no page cursor", result["handoff"])

    def test_input_bounds_and_reader_refusal(self):
        before = self.health_count()
        empty = run_job(self.base, "https://example.com", [], 40000, 1)
        self.assertEqual(empty["status"], "unsupported")
        too_many = run_job(self.base, "https://example.com", ["c%d" % n for n in range(33)], 40000, 1)
        self.assertEqual(too_many["status"], "unsupported")
        limit = run_job(self.base, FIXTURE_URL, FIELDS, 0, 1)
        self.assertEqual(limit["status"], "unsupported")
        huge = run_job(self.base, FIXTURE_URL, FIELDS, 40001, 1)
        self.assertEqual(huge["status"], "unsupported")
        invalid = run_job(self.base, "notaurl", FIELDS, 40000, 1)
        self.assertEqual(invalid["status"], "unsupported")
        self.assertEqual(invalid["retryable"], "false")
        self.assertIn("invalid_url", invalid["handoff"])
        private = run_job(self.base, "http://127.0.0.1/x", FIELDS, 40000, 1)
        self.assertEqual(private["status"], "unsupported")
        self.assertEqual(private["retryable"], "false")
        self.assertIn("ssrf_blocked", private["handoff"])
        self.assertGreater(self.health_count(), before)

    def test_malformed_boundary_body_does_not_count_as_read(self):
        before = self.health_count()
        request = urllib.request.Request(
            self.base + "/v1/read",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.health_count(), before)
        mapped = table_csv.main(FIXTURE_URL, json.dumps(FIELDS), 40000, 1, "not-json", 400)
        self.assertEqual(mapped["status"], "malformed")

    def test_interruption_and_explicit_retry(self):
        before = self.health_count()
        lost = run_job("http://127.0.0.1:1", FIXTURE_URL, FIELDS, 40000, 1)
        self.assertEqual(lost["status"], "transport")
        self.assertEqual(lost["retryable"], "true")
        self.assertEqual(lost["csv"], "")
        self.assertEqual(self.health_count(), before)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        sock.sendall(b"POST /v1/read HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 80\r\nConnection: close\r\n\r\n{\"url\":")
        sock.close()
        time.sleep(0.3)
        self.assertEqual(get_health(self.base)["ok"], True)
        self.assertEqual(self.health_count(), before)
        dns_before = self.health_count()
        first = run_job(self.base, "https://h64314-no-such-host.invalid/table", FIELDS, 40000, 1)
        second = run_job(self.base, "https://h64314-no-such-host.invalid/table", FIELDS, 40000, 1, explicit_retry=1)
        self.assertEqual(first["status"], "transport")
        self.assertEqual(second["status"], "transport")
        self.assertEqual(json.loads(second["provenance"])["explicit_retry"], 1)
        self.assertEqual(self.health_count() - dns_before, 2)
        recovered = run_job(self.base, "https://example.com", ["Title"], 40000, 1, explicit_retry=1)
        self.assertEqual(recovered["status"], "unsupported")
        self.assertIn("no HTML table", recovered["handoff"])

    def test_zz_write_comparison(self):
        started = time.perf_counter()
        status, body = post_read(self.base, FIXTURE_URL, 40000)
        direct = table_csv._pack(table_csv.evaluate(body, status, FIXTURE_URL, json.dumps(FIELDS), 40000, 1))
        graph = table_csv.main(FIXTURE_URL, json.dumps(FIELDS), 40000, 1, body, status)
        live = run_job(self.base, FIXTURE_URL, FIELDS, 40000, 1)
        page_before = self.health_count()
        page = run_job(self.base, FIXTURE_URL, FIELDS, 40000, 2)
        installed_path = ROOT / "evidence" / "installed-run.json"
        installed_http = "not_run"
        if installed_path.is_file():
            try:
                installed_http = json.loads(installed_path.read_text()).get("installed_http") or "not_run"
            except json.JSONDecodeError:
                installed_http = "not_run"
        comparison = {
            "label": "stage1_source_workflow_graph_and_direct_reader_http",
            "installed_dify": installed_http,
            "installed_http": installed_http,
            "owner_qa": True,
            "public_integration": False,
            "same_task": {
                "document_url": FIXTURE_URL,
                "fields": FIELDS,
                "markdown_limit": 40000,
                "page": 1,
            },
            "same_checker_bytes": graph == direct,
            "live_run_csv_matches_same_body": live["csv"] == direct["csv"] and live["status"] == direct["status"],
            "direct_reader_http": {
                "status": direct["status"],
                "setup_seconds": round(time.perf_counter() - started, 3),
                "completeness": json.loads(direct["provenance"])["completeness"],
            },
            "source_workflow_graph": {
                "status": graph["status"],
                "reader_url_in_dsl": READER_URL,
                "page_gate_request_delta": self.health_count() - page_before,
                "page_gate_status": page["status"],
                "note": "The graph code node and the direct checker ran on one reader body. The installed engine result is evidence/installed-run.json.",
            },
            "dimensions": {
                "setup": "Boundary listen plus one public read. No Dify image pull.",
                "pagination": "page 2 returns unsupported and does not increment requestCount.",
                "input_bounds": "Empty fields, 33 fields, limits 0 and 40001, and a 100 character clip are unsupported.",
                "malformed": "A non-JSON 200 is malformed. A non-JSON POST to the boundary is HTTP 400 and is not a read.",
                "network_interruption": "Closed port and DNS are transport. The test makes one explicit second DNS read and no hidden third read.",
                "recipient_usability": "CSV roundtrips through csv.reader. The handoff names the document, the truncation, and the next read.",
            },
            "w3c_tables_dropped_by_pinned_reader": getattr(self.__class__, "w3c_dropped_tables", None),
        }
        destination = Path(os.environ.get("DIFY_TABLE_COMPARISON", "")) if os.environ.get("DIFY_TABLE_COMPARISON") else None
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n")
        self.assertTrue(comparison["same_checker_bytes"])
        self.assertTrue(comparison["live_run_csv_matches_same_body"])
        self.assertEqual(comparison["source_workflow_graph"]["page_gate_request_delta"], 0)


class TestDirectCommand(unittest.TestCase):
    def test_help_and_usage(self):
        help_run = subprocess.run(
            [sys.executable, str(ROOT / "src" / "direct_csv.py"), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_run.returncode, 0, help_run.stderr)
        self.assertIn("--url", help_run.stdout)
        self.assertIn("--start-reader", help_run.stdout)
        usage = subprocess.run(
            [sys.executable, str(ROOT / "src" / "direct_csv.py")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(usage.returncode, 2, usage.stderr)
        self.assertIn("--url", usage.stderr)

    def test_page_two_does_not_call_a_closed_reader(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "src" / "direct_csv.py"),
                "--url",
                "https://example.com/table",
                "--fields",
                "Name,Value",
                "--page",
                "2",
                "--reader",
                "http://127.0.0.1:1",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn("status=unsupported", completed.stderr)
        self.assertEqual(completed.stdout, "")

    def test_command_source_has_no_proof_fixture(self):
        text = (ROOT / "src" / "direct_csv.py").read_text()
        self.assertNotIn("installed-proof", text)
        self.assertNotIn("FIXTURE_URL", text)
        self.assertNotIn("import tests", text)
        existing = (ROOT / "install" / "use-existing.py").read_text()
        self.assertNotIn("compose down", existing)
        self.assertNotIn("FIXTURE_URL", existing)
        self.assertIn("runtime_left_intact=1", existing)

    def test_admission_flag_allows_non_loopback_bind(self):
        if not (ROOT / "reader" / "node_modules" / "zod").is_dir():
            self.skipTest("reader dependencies are not installed")
        env = os.environ.copy()
        env.pop("H64314_ADMIT_DIFY", None)
        env["DIFY_TABLE_ADMIT"] = "1"
        env["READER_BIND"] = "0.0.0.0"
        env["PORT"] = "0"
        env["READER_CORE"] = str(ROOT / "reader")
        proc = subprocess.Popen(
            ["node", str(ROOT / "src" / "reader_boundary.mjs")],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        line = ""
        err = ""
        try:
            line = proc.stdout.readline()
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                _out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate(timeout=5)
        self.assertTrue(line.startswith("PORT "), err or line)

    def test_two_public_documents_differ(self):
        if not (ROOT / "reader" / "node_modules" / "zod").is_dir():
            self.skipTest("reader dependencies are not installed")
        import base64
        import hashlib
        fixture = "a21e38a7db1d0c5273ab356d9d85805c2fe453af6e6ef7b7eb894f3cc4700792"
        csvs = []
        for name, fields in (("samples/item-qty.html", "Item,Qty"), ("samples/item-note.html", "Item,Note")):
            body = (ROOT / name).read_bytes()
            url = "https://httpbin.org/base64/" + base64.urlsafe_b64encode(body).decode("ascii")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "src" / "direct_csv.py"),
                    "--url",
                    url,
                    "--fields",
                    fields,
                    "--start-reader",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("status=ok", completed.stderr)
            csvs.append(completed.stdout)
        self.assertNotEqual(csvs[0], csvs[1])
        for csv_text in csvs:
            self.assertNotEqual(hashlib.sha256(csv_text.encode()).hexdigest(), fixture)
            self.assertNotIn("Engine,Transpile,Reconcile", csv_text)


class TestOwnedRuntime(unittest.TestCase):
    def _run_install(self, extra):
        env = os.environ.copy()
        for key in (
            "H64314_ADMIT_DIFY",
            "DIFY_TABLE_ADMIT",
            "DIFY_SOURCE",
            "H64314_DIFY_SRC",
            "DIFY_TABLE_PROJECT",
            "DIFY_TABLE_KEEP",
            "DIFY_TABLE_STATE",
        ):
            env.pop(key, None)
        env.update(extra)
        return subprocess.run(
            ["bash", str(ROOT / "install" / "cold-install.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_existing_state_refused_before_effects(self):
        parent = tempfile.mkdtemp()
        try:
            state = os.path.join(parent, "state")
            os.mkdir(state, 0o755)
            marker = os.path.join(state, "marker")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("UNCHANGED_STATE_MARKER\n")
            mode = stat.S_IMODE(os.lstat(state).st_mode)
            completed = self._run_install({"DIFY_TABLE_ADMIT": "1", "DIFY_TABLE_STATE": state})
            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            self.assertIn("refuse: state directory already exists", completed.stdout)
            self.assertNotIn("docker_pull_begin", completed.stdout)
            self.assertEqual(stat.S_IMODE(os.lstat(state).st_mode), mode)
            self.assertEqual(Path(marker).read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
            link = os.path.join(parent, "link")
            os.symlink(state, link)
            linked = self._run_install({"DIFY_TABLE_ADMIT": "1", "DIFY_TABLE_STATE": link})
            self.assertEqual(linked.returncode, 2, linked.stdout + linked.stderr)
            self.assertIn("refuse: state directory already exists", linked.stdout)
            self.assertEqual(Path(marker).read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
        finally:
            shutil.rmtree(parent)

    def _owner(self, state, pid, starttime, project="difytablecsv-abc12345"):
        token = "owned-test-token"
        st = os.lstat(state)
        doc = {
            "schema": "dify-table-csv.owned-runtime.v1",
            "owned_by": "dify-table-csv",
            "token": token,
            "state_dev": st.st_dev,
            "state_ino": st.st_ino,
            "project": project,
            "reader_pid": pid,
            "reader_starttime": starttime,
            "reader_boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "reader_container": "",
            "container_ids": [],
            "network_ids": [],
        }
        owner = os.path.join(state, "owner.json")
        with open(owner, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
            handle.write("\n")
        os.chmod(owner, 0o600)
        token_path = os.path.join(state, "owner.token")
        with open(token_path, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.chmod(token_path, 0o600)
        with open(os.path.join(state, "reader.pid"), "w", encoding="utf-8") as handle:
            handle.write(str(pid) + "\n")
        with open(os.path.join(state, "marker"), "w", encoding="utf-8") as handle:
            handle.write("UNCHANGED_STATE_MARKER\n")

    def _starttime(self, pid):
        raw = Path("/proc/%s/stat" % pid).read_text(encoding="utf-8")
        return raw[raw.rfind(")") + 2 :].split()[19]

    def test_stop_refuses_foreign_directory_and_reused_pid(self):
        parent = tempfile.mkdtemp()
        sentinel = None
        try:
            foreign = os.path.join(parent, "foreign")
            os.mkdir(foreign)
            with open(os.path.join(foreign, "marker"), "w", encoding="utf-8") as handle:
                handle.write("UNCHANGED_STATE_MARKER\n")
            missing = subprocess.run(
                ["bash", str(ROOT / "install" / "stop-owned-runtime.sh"), foreign],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
            self.assertIn("refuse: owned runtime state is missing", missing.stdout)
            self.assertEqual(Path(foreign, "marker").read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
            binary = os.path.join(parent, "reader_boundary.mjs")
            shutil.copy(shutil.which("sleep"), binary)
            os.chmod(binary, 0o755)
            sentinel = subprocess.Popen([binary, "300"])
            state = os.path.join(parent, "owned")
            os.mkdir(state, 0o700)
            self._owner(state, sentinel.pid, "1")
            reused = subprocess.run(
                ["bash", str(ROOT / "install" / "stop-owned-runtime.sh"), state],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
            self.assertIn("reader_pid_reused=1", reused.stdout)
            self.assertIsNone(sentinel.poll())
            self.assertEqual(Path(state, "marker").read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
            moved = os.path.join(parent, "moved")
            os.mkdir(moved)
            shutil.copy(os.path.join(state, "owner.json"), os.path.join(moved, "owner.json"))
            shutil.copy(os.path.join(state, "owner.token"), os.path.join(moved, "owner.token"))
            with open(os.path.join(moved, "marker"), "w", encoding="utf-8") as handle:
                handle.write("UNCHANGED_STATE_MARKER\n")
            changed = subprocess.run(
                ["bash", str(ROOT / "install" / "stop-owned-runtime.sh"), moved],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(changed.returncode, 2, changed.stdout + changed.stderr)
            self.assertIn("refuse: state directory identity changed", changed.stdout)
            self.assertIsNone(sentinel.poll())
            self.assertEqual(Path(moved, "marker").read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
        finally:
            if sentinel is not None and sentinel.poll() is None:
                sentinel.kill()
                sentinel.wait(timeout=5)
            shutil.rmtree(parent)

    def test_stop_removes_only_the_matching_reader_process(self):
        parent = tempfile.mkdtemp()
        proc = None
        try:
            binary = os.path.join(parent, "reader_boundary.mjs")
            shutil.copy(shutil.which("sleep"), binary)
            os.chmod(binary, 0o755)
            proc = subprocess.Popen([binary, "300"])
            state = os.path.join(parent, "owned")
            os.mkdir(state, 0o700)
            self._owner(state, proc.pid, self._starttime(proc.pid), project="difytablecsv-def67890")
            completed = subprocess.run(
                ["bash", str(ROOT / "install" / "stop-owned-runtime.sh"), state],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("reader_stopped=1", completed.stdout)
            self.assertIn("stack_stopped=1", completed.stdout)
            proc.wait(timeout=5)
            self.assertEqual(Path(state, "marker").read_text(encoding="utf-8"), "UNCHANGED_STATE_MARKER\n")
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            shutil.rmtree(parent)

    def test_export_omits_untracked_sentinel_and_refuses_symlink(self):
        parent = tempfile.mkdtemp()
        try:
            src = os.path.join(parent, "src")
            os.makedirs(os.path.join(src, "docker", "volumes", "db", "data"))
            with open(os.path.join(src, "docker", "docker-compose.yaml"), "w", encoding="utf-8") as handle:
                handle.write("services: {}\n")
            sentinel = os.path.join(src, "docker", "volumes", "db", "data", "LIVE_SENTINEL")
            with open(sentinel, "w", encoding="utf-8") as handle:
                handle.write("LIVE_SENTINEL\n")
            outside = os.path.join(parent, "outside")
            os.makedirs(outside)
            secret = os.path.join(outside, "secret")
            with open(secret, "w", encoding="utf-8") as handle:
                handle.write("OUTSIDE_SECRET_BYTES\n")
            os.makedirs(os.path.join(src, "docker", "volumes", "app"))
            link = os.path.join(src, "docker", "volumes", "app", "storage")
            os.symlink(outside, link)
            subprocess.run(["git", "-C", src, "init", "-q"], check=True)
            subprocess.run(["git", "-C", src, "config", "user.email", "probe@example.com"], check=True)
            subprocess.run(["git", "-C", src, "config", "user.name", "Probe"], check=True)
            subprocess.run(["git", "-C", src, "add", "docker/docker-compose.yaml"], check=True)
            subprocess.run(["git", "-C", src, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], check=True)
            head = subprocess.check_output(["git", "-C", src, "rev-parse", "HEAD"], text=True).strip()
            dest = os.path.join(parent, "owned")
            exported = subprocess.run(
                ["bash", str(ROOT / "install" / "export-tracked-docker.sh"), src, dest, head],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(exported.returncode, 0, exported.stdout + exported.stderr)
            self.assertIn("tracked_export=1", exported.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dest, "docker", "docker-compose.yaml")))
            self.assertFalse(os.path.exists(os.path.join(dest, "docker", "volumes", "db", "data", "LIVE_SENTINEL")))
            self.assertFalse(os.path.lexists(os.path.join(dest, "docker", "volumes", "app", "storage")))
            self.assertEqual(Path(sentinel).read_text(encoding="utf-8"), "LIVE_SENTINEL\n")
            self.assertEqual(os.readlink(link), outside)
            self.assertEqual(Path(secret).read_text(encoding="utf-8"), "OUTSIDE_SECRET_BYTES\n")
            linked = os.path.join(parent, "linked")
            os.makedirs(os.path.join(linked, "docker"))
            with open(os.path.join(linked, "docker", "docker-compose.yaml"), "w", encoding="utf-8") as handle:
                handle.write("services: {}\n")
            os.symlink(secret, os.path.join(linked, "docker", "leak"))
            subprocess.run(["git", "-C", linked, "init", "-q"], check=True)
            subprocess.run(["git", "-C", linked, "config", "user.email", "probe@example.com"], check=True)
            subprocess.run(["git", "-C", linked, "config", "user.name", "Probe"], check=True)
            subprocess.run(["git", "-C", linked, "add", "docker"], check=True)
            subprocess.run(["git", "-C", linked, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], check=True)
            linked_head = subprocess.check_output(["git", "-C", linked, "rev-parse", "HEAD"], text=True).strip()
            refused = subprocess.run(
                ["bash", str(ROOT / "install" / "export-tracked-docker.sh"), linked, os.path.join(parent, "refused"), linked_head],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn("refuse: tracked tree contains a symlink", refused.stdout + refused.stderr)
            self.assertFalse(os.path.exists(os.path.join(parent, "refused")))
            self.assertEqual(Path(secret).read_text(encoding="utf-8"), "OUTSIDE_SECRET_BYTES\n")
        finally:
            shutil.rmtree(parent)

    def test_same_bridge_probe_requires_a_listener_token(self):
        probe = (ROOT / "install" / "owned-runtime-probe.sh").read_text(encoding="utf-8")
        self.assertIn("same-bridge-open", probe)
        self.assertIn("probe_refused=same_bridge_unpublished_closed", probe)
        self.assertIn("sudo -n rm -rf \"$work\"", probe)
        self.assertIn("-A DOCKER ! -i ${bridge_if} -o ${bridge_if} -j DROP", probe)
        self.assertIn("filter_docker_drop=1", probe)
        self.assertNotIn("raw_prerouting_drop", probe)
        self.assertNotIn("kill -0 \"$pid\"", probe)
        stop = (ROOT / "install" / "stop-owned-runtime.sh").read_text(encoding="utf-8")
        start = stop.index("An empty recorded list is still a record")
        foreign = stop.index("stop_refused=foreign_container", start)
        self.assertNotIn('-s "$state/stop.containers"', stop[start:foreign])


if __name__ == "__main__":
    unittest.main()
