#!/usr/bin/env python3
"""Print CSV and provenance for one public document.

This command uses the packed checker and reader. It does not start Dify and
it does not call a model. A stopped reader returns no CSV.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from direct_client import get_health, run_job  # noqa: E402


def usage(parser, message):
    print("error: " + message, file=sys.stderr)
    parser.print_help(sys.stderr)
    raise SystemExit(2)


def parse_fields(raw):
    text = raw.strip()
    if text.startswith("["):
        parsed = json.loads(text)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("fields JSON must be a list of strings")
        return parsed
    fields = [item.strip() for item in text.split(",") if item.strip()]
    if not fields:
        raise ValueError("at least one field is required")
    return fields


def start_reader(core):
    env = dict(**{key: value for key, value in __import__("os").environ.items()})
    env["READER_CORE"] = str(core)
    env["READER_BIND"] = "127.0.0.1"
    env["PORT"] = "0"
    proc = subprocess.Popen(
        ["node", str(SRC / "reader_boundary.mjs")],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline() if proc.stdout is not None else ""
    if not line.startswith("PORT "):
        err = proc.stderr.read() if proc.stderr is not None else ""
        proc.kill()
        print("error: reader did not start", file=sys.stderr)
        if err:
            print(err.strip(), file=sys.stderr)
        raise SystemExit(5)
    port = int(line.split()[1])
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            print("error: reader exited before it was ready", file=sys.stderr)
            raise SystemExit(5)
        try:
            get_health(base, timeout=1)
            return proc, base
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.1)
    proc.kill()
    print("error: reader health did not answer", file=sys.stderr)
    raise SystemExit(5)


def emit(result):
    status = result.get("status")
    csv_text = result.get("csv") if isinstance(result.get("csv"), str) else ""
    provenance = result.get("provenance")
    print(csv_text, end="" if csv_text.endswith("\n") or csv_text == "" else "\n")
    print(
        "status=%s retryable=%s provenance=%s"
        % (status, json.dumps(result.get("retryable")), json.dumps(provenance, ensure_ascii=False, sort_keys=True)),
        file=sys.stderr,
    )
    if status == "ok":
        return 0
    if status == "unsupported":
        return 3
    if status == "transport":
        return 5
    return 4


def main(argv):
    parser = argparse.ArgumentParser(
        prog="direct_csv.py",
        description="Read one public document URL into CSV and provenance. Does not start Dify.",
    )
    parser.add_argument("--url", help="Public document URL")
    parser.add_argument("--fields", help="Comma-separated field names, or a JSON list of strings")
    parser.add_argument("--reader", default="http://127.0.0.1:8765", help="Reader base URL")
    parser.add_argument("--page", default="1")
    parser.add_argument("--markdown-limit", default="40000")
    parser.add_argument("--start-reader", action="store_true", help="Start the packed reader on 127.0.0.1 and stop it on exit")
    parser.add_argument("--reader-core", default=str(ROOT / "reader"))
    args = parser.parse_args(argv)
    if not args.url:
        usage(parser, "--url is required")
    if not args.fields:
        usage(parser, "--fields is required")
    try:
        fields = parse_fields(args.fields)
        page = int(args.page)
        limit = int(args.markdown_limit)
    except (ValueError, json.JSONDecodeError) as exc:
        usage(parser, str(exc))
    proc = None
    base = args.reader
    try:
        if args.start_reader:
            proc, base = start_reader(Path(args.reader_core))
        result = run_job(base, args.url, fields, limit, page)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
