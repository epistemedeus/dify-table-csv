"""Direct reader HTTP client.

The page gate matches the workflow: page other than 1 does not call the reader.
A lost connection is transport and is not retried here. A test may call again
and pass explicit_retry=1. That second call is a new read.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from table_csv import _as_int, _base_provenance, _fields, _handoff, _pack, evaluate


def post_read(base_url, document_url, markdown_limit, timeout=40):
    payload = json.dumps(
        {"url": document_url, "markdownLimitChars": markdown_limit},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/read",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def get_health(base_url, timeout=5):
    with urllib.request.urlopen(base_url.rstrip("/") + "/health", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _connection_failure(document_url, fields_json, markdown_limit, page, explicit_retry, detail):
    provenance = _base_provenance(
        document_url if isinstance(document_url, str) else "",
        _as_int(page),
        _as_int(explicit_retry) or 0,
        _as_int(markdown_limit),
    )
    fields, _field_error = _fields(fields_json)
    if fields is not None:
        provenance["requested_fields"] = fields
    provenance["retryable"] = True
    result = {
        "status": "transport",
        "csv": "",
        "provenance": provenance,
        "handoff": _handoff("transport", provenance["document_url"], detail, "", provenance),
        "retryable": True,
    }
    return _pack(result)


def run_job(base_url, document_url, fields, markdown_limit, page, explicit_retry=0, timeout=40):
    fields_json = fields if isinstance(fields, str) else json.dumps(fields, ensure_ascii=False)
    if _as_int(page) != 1:
        return _pack(
            evaluate(
                body="",
                status_code=0,
                document_url=document_url,
                fields_json=fields_json,
                markdown_limit=markdown_limit,
                page=page,
                explicit_retry=explicit_retry,
            )
        )
    try:
        status, body = post_read(base_url, document_url, markdown_limit, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _connection_failure(
            document_url,
            fields_json,
            markdown_limit,
            page,
            explicit_retry,
            "The reader HTTP call failed before a response (%s)." % exc.__class__.__name__,
        )
    return _pack(
        evaluate(
            body=body,
            status_code=status,
            document_url=document_url,
            fields_json=fields_json,
            markdown_limit=markdown_limit,
            page=page,
            explicit_retry=explicit_retry,
        )
    )
