"""Checked CSV and handoff for one free reader record.

The Dify code node embeds the marked sections. It does not call a model and it
does not invent a grid for a table the reader kept only as combined spans.
"""

from __future__ import annotations

# DIFY_SHARED_BEGIN
import csv
import io
import json
import re
from html.parser import HTMLParser

READER_MERCHANT_COMMIT = "a1dc09bb076b823fb3492ea79fa10f8602ed01a5"
READER_CORE_COMMIT = "38f8787b04bf3221ee0875ce1a6af687592c8015"
CHECKER_ID = "h64314.table_csv.v1"
PROVENANCE_SCHEMA = "h64314.reader-csv.provenance.v1"
MAX_FIELDS = 32
MAX_FIELD_LEN = 80
READER_KEYS = (
    "ok",
    "requestedUrl",
    "finalUrl",
    "url",
    "status",
    "sourceOk",
    "error",
    "title",
    "markdown",
    "wordCount",
    "truncated",
    "capture",
    "fetchedAt",
)
OMITTED_RE = re.compile(r"\[Table omitted: ([^;\]]+); cell mappings unavailable\.\]")
INLINE_TAGS = {"a", "span", "sup", "sub", "code", "em", "strong", "b", "i", "br", "p", "small"}

def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value or ""):
        return int(value)
    return None


def _pack(result):
    provenance = json.dumps(result["provenance"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "status": result["status"],
        "csv": result["csv"],
        "provenance": provenance,
        "handoff": result["handoff"],
        "retryable": "true" if result["retryable"] else "false",
    }


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.kinds = []
        self.reason = None
        self._row = None
        self._kinds = None
        self._cell = None
        self._cell_kind = None
        self._ignore = 0

    def handle_starttag(self, tag, attrs):
        attr = {key.lower(): value for key, value in attrs}
        if tag in ("td", "th"):
            if self._row is None:
                self.reason = self.reason or "cell outside a row"
                self._ignore += 1
                return
            span_bad = False
            for name in ("colspan", "rowspan"):
                if name in attr and attr[name] != "1":
                    span_bad = True
            if span_bad:
                self.reason = self.reason or "combined spans"
            self._cell = []
            self._cell_kind = tag
            return
        if tag == "tr":
            self._row = []
            self._kinds = []
            return
        if tag in ("table", "thead", "tbody", "tfoot", "caption", "colgroup", "col"):
            return
        if tag in INLINE_TAGS:
            if tag == "br" and self._cell is not None:
                self._cell.append(" ")
            return
        if self._cell is not None or self._row is not None:
            self.reason = self.reason or "unsupported table markup"

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self._ignore:
                self._ignore -= 1
                return
            if self._cell is None or self._row is None:
                self.reason = self.reason or "unclosed or malformed table"
                return
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.append(text)
            self._kinds.append(self._cell_kind)
            self._cell = None
            self._cell_kind = None
            return
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self.kinds.append(self._kinds)
            self._row = None
            self._kinds = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _tables(markdown):
    found = []
    for match in OMITTED_RE.finditer(markdown):
        found.append({"kind": "omitted", "reason": match.group(1), "start": match.start()})
    for match in re.finditer(r"<table\b[^>]*>.*?</table>", markdown, flags=re.IGNORECASE | re.DOTALL):
        parser = _TableParser()
        try:
            parser.feed(match.group(0))
            parser.close()
        except Exception as exc:
            found.append({"kind": "malformed_table", "reason": str(exc), "start": match.start()})
            continue
        if parser.reason:
            found.append({"kind": "unsupported_table", "reason": parser.reason, "start": match.start(), "html": match.group(0)})
            continue
        if not parser.rows:
            found.append({"kind": "unsupported_table", "reason": "table has no rows", "start": match.start()})
            continue
        header, header_kinds = parser.rows[0], parser.kinds[0]
        if not header_kinds or any(kind != "th" for kind in header_kinds):
            found.append({"kind": "unsupported_table", "reason": "first row is not a header of th cells", "start": match.start()})
            continue
        if len(header) != len(set(header)) or any(not cell for cell in header):
            found.append({"kind": "unsupported_table", "reason": "header is empty or repeated", "start": match.start()})
            continue
        width = len(header)
        data = []
        ragged = False
        for row, kinds in zip(parser.rows[1:], parser.kinds[1:]):
            if len(row) != width or any(kind != "td" for kind in kinds):
                ragged = True
                break
            data.append(row)
        if ragged:
            found.append({"kind": "unsupported_table", "reason": "ragged or mixed data row", "start": match.start()})
            continue
        found.append({
            "kind": "rectangular",
            "header": header,
            "rows": data,
            "start": match.start(),
            "html": match.group(0),
        })
    found.sort(key=lambda item: item["start"])
    return found


def _fields(fields_json):
    if isinstance(fields_json, list):
        parsed = fields_json
    elif isinstance(fields_json, str):
        try:
            parsed = json.loads(fields_json)
        except json.JSONDecodeError:
            return None, "fields_json is not a JSON array"
    else:
        return None, "fields_json is not a JSON array"
    if not isinstance(parsed, list) or not parsed:
        return None, "requested fields are empty"
    if len(parsed) > MAX_FIELDS:
        return None, "requested fields exceed %d" % MAX_FIELDS
    if len(parsed) != len(set(parsed)):
        return None, "requested fields repeat"
    for field in parsed:
        if not isinstance(field, str) or not field or len(field) > MAX_FIELD_LEN:
            return None, "a requested field is empty or too long"
        if any(ord(ch) < 32 for ch in field):
            return None, "a requested field contains a control character"
    return parsed, None


def _csv(header, rows):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def _handoff(status, document_url, detail, csv_text, provenance):
    lines = [
        "Reader table handoff",
        "",
        "Status: %s." % status,
        "Document: %s" % (document_url or "(none)"),
        detail,
    ]
    if status == "ok":
        lines.append("The CSV output is the requested columns from the first intact rectangular table.")
        if provenance.get("reader_truncated"):
            lines.append("The reader marked this document truncated. The CSV is not a full-document extract. Rows after an omitted or clipped region are absent.")
        lines.append("Open the CSV in a spreadsheet. Empty cells are empty source cells, not missing files.")
        lines.append("A later read is a new request. This result does not retry itself.")
        lines.append("CSV data rows: %d." % provenance.get("row_count", 0))
    else:
        lines.append("No CSV was produced. This is not a repaired table and not a partial grid.")
        lines.append("Next action: use a public http(s) document whose reader record contains a rectangular header row, and request those header names as a JSON array.")
        lines.append("Page numbers other than 1 are unsupported. The free reader has no page cursor.")
        if provenance.get("retryable"):
            lines.append("The failure was transport. One explicit new read is appropriate. Do not treat a completed refusal as a transport retry.")
        else:
            lines.append("A completed refusal is not retried automatically. The reader contract allows only an explicit new read.")
    return "\n".join(lines).rstrip() + "\n"


def _base_provenance(document_url, page, explicit_retry, markdown_limit):
    return {
        "schema": PROVENANCE_SCHEMA,
        "checker": CHECKER_ID,
        "reader_merchant_commit": READER_MERCHANT_COMMIT,
        "reader_core_commit": READER_CORE_COMMIT,
        "reader_entrypoint": "readMarkdown",
        "document_url": document_url,
        "page": page,
        "requested_markdown_limit": markdown_limit,
        "explicit_retry": explicit_retry,
        "reader_truncated": None,
        "completeness": None,
        "selected_table_index": None,
        "requested_fields": None,
        "row_count": 0,
        "omitted": [],
        "unsupported_tables": [],
        "retryable": False,
    }


def evaluate(body, status_code, document_url, fields_json, markdown_limit, page, explicit_retry=0):
    page_n = _as_int(page)
    limit_n = _as_int(markdown_limit)
    status_n = _as_int(status_code)
    retry_n = _as_int(explicit_retry)
    provenance = _base_provenance(document_url if isinstance(document_url, str) else "", page_n, retry_n or 0, limit_n)
    fields, field_error = _fields(fields_json)

    def finish(status, detail, csv_text="", retryable=False):
        provenance["retryable"] = bool(retryable)
        if fields is not None:
            provenance["requested_fields"] = fields
        return {
            "status": status,
            "csv": csv_text,
            "provenance": provenance,
            "handoff": _handoff(status, provenance["document_url"], detail, csv_text, provenance),
            "retryable": bool(retryable),
        }

    if not isinstance(document_url, str) or not document_url:
        return finish("unsupported", "The document URL is missing.")
    if page_n != 1:
        return finish("unsupported", "Requested page %s is unsupported. The free reader returns one bounded record and has no page cursor." % page)
    if limit_n is None or not 1 <= limit_n <= 40000:
        return finish("unsupported", "markdown_limit must be an integer from 1 through 40000.")
    if field_error:
        return finish("unsupported", field_error + ".")
    if status_n is None:
        return finish("malformed", "The reader HTTP status was missing.")
    if status_n == 0:
        return finish("malformed", "The reader was not called, but page 1 was requested.")
    payload = None
    if isinstance(body, str) and body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
            if status_n == 200:
                return finish("malformed", "The reader body was not JSON.")
    elif isinstance(body, dict):
        payload = body
    if status_n == 422 and isinstance(payload, dict):
        code = ((payload.get("error") or {}) if isinstance(payload.get("error"), dict) else {}).get("code") or "rejected"
        return finish("unsupported", "The reader refused the input before a document record (%s)." % code)
    if status_n == 502:
        return finish("transport", "The reader HTTP call failed in transport before a complete record.", retryable=True)
    if status_n != 200 or not isinstance(payload, dict):
        return finish("malformed", "The reader HTTP result was not a 200 JSON record (status %s)." % status_n)
    if payload.get("ok") is not True or any(key not in payload for key in READER_KEYS):
        return finish("malformed", "The JSON is not the free reader's 13-field record.")
    provenance["requested_url"] = payload.get("requestedUrl")
    provenance["final_url"] = payload.get("finalUrl")
    provenance["source_status"] = payload.get("status")
    provenance["source_ok"] = payload.get("sourceOk")
    provenance["fetched_at"] = payload.get("fetchedAt")
    provenance["title"] = payload.get("title")
    provenance["capture"] = payload.get("capture")
    provenance["reader_truncated"] = bool(payload.get("truncated"))
    if payload.get("sourceOk") is not True:
        err = payload.get("error")
        return finish("source_refused", "The source did not return a usable document (%s)." % (err if err else payload.get("status")))
    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        return finish("malformed", "The reader record has no markdown string.")
    tables = _tables(markdown)
    provenance["omitted"] = [item["reason"] for item in tables if item["kind"] == "omitted"]
    provenance["unsupported_tables"] = [item["reason"] for item in tables if item["kind"] != "rectangular" and item["kind"] != "omitted"]
    rectangular = [item for item in tables if item["kind"] == "rectangular"]
    chosen = None
    chosen_index = None
    for index, item in enumerate(rectangular):
        if all(field in item["header"] for field in fields):
            chosen = item
            chosen_index = index
            break
    if chosen is None:
        if any(reason == "combined spans" for reason in provenance["unsupported_tables"]):
            detail = "The reader preserved a table with combined spans. This checker does not guess a rectangular grid from spans, so no CSV was built."
        elif provenance["reader_truncated"] and not rectangular:
            detail = "The reader truncated the document before an intact rectangular table was available."
        elif not rectangular and not tables:
            detail = "The reader record contains no HTML table."
        else:
            detail = "No intact rectangular table contains every requested field."
        return finish("unsupported", detail)
    positions = [chosen["header"].index(field) for field in fields]
    projected = [[row[pos] for pos in positions] for row in chosen["rows"]]
    csv_text = _csv(fields, projected)
    provenance["selected_table_index"] = chosen_index
    provenance["row_count"] = len(projected)
    provenance["completeness"] = "selected_table" if provenance["reader_truncated"] else "reader_record"
    detail = "Selected rectangular table %d with %d data rows and fields %s." % (chosen_index, len(projected), ", ".join(fields))
    return finish("ok", detail, csv_text)
# DIFY_SHARED_END


def main(document_url: str, fields_json: str, markdown_limit, page, body: str, status_code):
    return _pack(evaluate(
        body=body,
        status_code=status_code,
        document_url=document_url,
        fields_json=fields_json,
        markdown_limit=markdown_limit,
        page=page,
        explicit_retry=0,
    ))


REFUSE_MAIN = """
def main(document_url: str, fields_json: str, markdown_limit, page):
    return _pack(evaluate(body="", status_code=0, document_url=document_url, fields_json=fields_json, markdown_limit=markdown_limit, page=page, explicit_retry=0))
""".strip() + "\n"
