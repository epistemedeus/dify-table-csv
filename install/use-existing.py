#!/usr/bin/env python3
"""Import and run the packed workflow on a Dify 1.18 runtime that is already up.

This command does not start compose, does not stop containers, and does not
substitute a fixture document. The operator supplies the document URL and fields.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from direct_client import run_job  # noqa: E402

PINNED_READER_URL = "http://host.docker.internal:8765/v1/read"
DSL = ROOT / "workflow" / "reader-table-csv.dsl.yml"


def fail(message):
    print("error: " + message, file=sys.stderr)
    print("runtime_left_intact=1")
    raise SystemExit(1)


def load_admin(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    for key in ("email", "name", "password", "init_password"):
        if not values.get(key):
            fail("admin file is missing %s" % key)
    return values


def parse_fields(raw):
    text = raw.strip()
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("fields JSON must be a list of strings")
        return parsed
    return [item.strip() for item in text.split(",") if item.strip()]


class Console:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
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
                parsed = {"body_omitted": True}
            if isinstance(parsed, dict):
                for key in ("password", "access_token", "secret_key", "api_key", "token"):
                    parsed.pop(key, None)
            return exc.code, parsed


def import_yaml(console, yaml_text):
    code, imported = console.request(
        "POST",
        "/console/api/apps/imports",
        {"mode": "yaml-content", "yaml_content": yaml_text},
    )
    if code not in (200, 201) or not isinstance(imported, dict):
        fail("import failed")
    if imported.get("status") == "pending":
        code, imported = console.request("POST", "/console/api/apps/imports/%s/confirm" % imported["id"])
        if code not in (200, 201) or not isinstance(imported, dict):
            fail("import confirm failed")
    if imported.get("status") not in ("completed", "completed-with-warnings") or not imported.get("app_id"):
        fail("import status %s" % imported.get("status"))
    return imported["app_id"]


def login(console, admin):
    status, body = console.request("GET", "/console/api/setup")
    if status != 200 or not isinstance(body, dict):
        fail("setup status failed")
    init_status, init_body = console.request("GET", "/console/api/init")
    if init_status == 200 and isinstance(init_body, dict) and init_body.get("status") == "not_started":
        code, _init_post = console.request("POST", "/console/api/init", {"password": admin["init_password"]})
        if code not in (200, 201):
            fail("init validate failed")
    if body.get("step") == "not_started":
        code, _setup_body = console.request(
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
            fail("setup failed")
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
        fail("login failed")
    data = login_body.get("data") if isinstance(login_body.get("data"), dict) else {}
    token = data.get("access_token") or login_body.get("access_token")
    if not token:
        for cookie in console.jar:
            if cookie.name == "access_token" and cookie.value:
                token = cookie.value.strip().strip('"')
                break
    if not token:
        fail("login returned no access token")
    console.token = token


def main(argv):
    parser = argparse.ArgumentParser(
        prog="use-existing.py",
        description="Import and run the packed table workflow on an existing Dify 1.18 runtime.",
    )
    parser.add_argument("--console", required=True, help="Console origin, for example http://127.0.0.1:5001")
    parser.add_argument("--admin-file", required=True)
    parser.add_argument("--url", required=True, help="Document URL supplied by the operator")
    parser.add_argument("--fields", required=True)
    parser.add_argument("--reader-base", default="http://127.0.0.1:8765")
    parser.add_argument("--reader-url", default=PINNED_READER_URL, help="Reader URL written into the imported workflow copy")
    parser.add_argument("--page", default="1")
    parser.add_argument("--markdown-limit", default="40000")
    parser.add_argument("--app-id", default="", help="Reuse an app imported by an earlier run")
    parser.add_argument("--i-own-this-runtime", action="store_true")
    args = parser.parse_args(argv)
    host = urlparse(args.console).hostname
    if host not in {"127.0.0.1", "localhost"} and not args.i_own_this_runtime:
        print("error: pass --i-own-this-runtime for a console that is not loopback", file=sys.stderr)
        print("runtime_left_intact=1")
        return 2
    try:
        fields = parse_fields(args.fields)
        page = int(args.page)
        limit = int(args.markdown_limit)
    except (ValueError, json.JSONDecodeError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        print("runtime_left_intact=1")
        return 2
    if not fields:
        print("error: at least one field is required", file=sys.stderr)
        print("runtime_left_intact=1")
        return 2
    yaml_text = DSL.read_text()
    if PINNED_READER_URL not in yaml_text:
        fail("packed workflow has no pinned reader URL")
    if "\n" in args.reader_url or "\r" in args.reader_url:
        fail("reader URL must be one line")
    yaml_text = yaml_text.replace(PINNED_READER_URL, args.reader_url, 1)
    admin = load_admin(args.admin_file)
    console = Console(args.console)
    login(console, admin)
    app_id = args.app_id or import_yaml(console, yaml_text)
    if not args.app_id:
        publish_code, _published = console.request(
            "POST",
            "/console/api/apps/%s/workflows/publish" % app_id,
            {"marked_name": "table-csv", "marked_comment": "operator run"},
        )
        if publish_code not in (200, 201):
            publish_code, _published = console.request(
                "POST",
                "/console/api/apps/%s/workflows/publish" % app_id,
                {},
            )
        if publish_code not in (200, 201):
            fail("publish failed")
    key_code, key_body = console.request("POST", "/console/api/apps/%s/api-keys" % app_id, {})
    if key_code not in (200, 201) or not isinstance(key_body, dict) or not key_body.get("token"):
        fail("api key create failed")
    app_token = key_body["token"]
    code, body = console.request(
        "POST",
        "/v1/workflows/run",
        {
            "inputs": {
                "document_url": args.url,
                "fields_json": json.dumps(fields),
                "markdown_limit": limit,
                "page": page,
            },
            "response_mode": "blocking",
            "user": "table-csv",
        },
        token=app_token,
        timeout=180,
    )
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else {}
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    csv_text = outputs.get("csv") if isinstance(outputs.get("csv"), str) else ""
    direct = run_job(args.reader_base, args.url, fields, limit, page)
    digest = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    result = {
        "http_status": code,
        "workflow_status": data.get("status"),
        "output_status": outputs.get("status"),
        "total_tokens": data.get("total_tokens"),
        "retryable": outputs.get("retryable"),
        "csv_sha256": digest,
        "csv_bytes": len(csv_text.encode("utf-8")),
        "direct_status": direct.get("status"),
        "direct_csv_sha256": hashlib.sha256((direct.get("csv") or "").encode("utf-8")).hexdigest(),
        "direct_equal": csv_text == direct.get("csv") and outputs.get("status") == direct.get("status"),
        "app_id": app_id,
    }
    print("normal_result=" + json.dumps(result, sort_keys=True))
    print(csv_text, end="" if csv_text.endswith("\n") or csv_text == "" else "\n")
    print("runtime_left_intact=1")
    print("app_id=%s" % app_id)
    if code != 200 or data.get("status") != "succeeded":
        return 1
    if result["output_status"] == "ok" and result["total_tokens"] in (0, "0", 0.0) and result["direct_equal"]:
        return 0
    if result["output_status"] == "unsupported":
        return 3
    if result["output_status"] == "transport":
        return 5
    return 4


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
