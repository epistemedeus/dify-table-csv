"""Build the importable Dify 0.7.0 workflow from the shared checker source."""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

import table_csv  # noqa: E402

READER_URL = "http://host.docker.internal:8765/v1/read"
FORBIDDEN = {
    "llm",
    "agent",
    "knowledge-retrieval",
    "knowledge-index",
    "question-classifier",
    "parameter-extractor",
    "tool",
}
OUTPUTS = ("status", "csv", "provenance", "handoff", "retryable")


def shared_source() -> str:
    text = Path(table_csv.__file__).read_text()
    start = text.index("# DIFY_SHARED_BEGIN\n") + len("# DIFY_SHARED_BEGIN\n")
    end = text.index("\n# DIFY_SHARED_END\n")
    return text[start:end].strip() + "\n"


def check_code() -> str:
    return shared_source() + "\n" + inspect.getsource(table_csv.main)


def refuse_code() -> str:
    return shared_source() + "\n" + table_csv.REFUSE_MAIN


def _string_outputs():
    return {name: {"children": None, "type": "string"} for name in OUTPUTS}


def _end_outputs(source):
    return [
        {"value_selector": [source, name], "value_type": "string", "variable": name}
        for name in OUTPUTS
    ]


def _node(node_id, data, x, y, height=116):
    data = dict(data)
    data.setdefault("desc", "")
    data.setdefault("selected", False)
    data.setdefault("version", "1")
    return {
        "data": data,
        "height": height,
        "id": node_id,
        "position": {"x": x, "y": y},
        "positionAbsolute": {"x": x, "y": y},
        "selected": False,
        "sourcePosition": "right",
        "targetPosition": "left",
        "type": "custom",
        "width": 244,
    }


def _edge(source, target, source_type, target_type, source_handle="source"):
    return {
        "data": {
            "isInIteration": False,
            "isInLoop": False,
            "sourceType": source_type,
            "targetType": target_type,
        },
        "id": "%s-%s-%s" % (source, source_handle, target),
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": "target",
        "type": "custom",
        "zIndex": 0,
    }


def _start_variable(variable, label, type_name, max_length):
    return {
        "label": label,
        "max_length": max_length,
        "options": [],
        "required": True,
        "type": type_name,
        "variable": variable,
    }


def build_document():
    check = check_code()
    refuse = refuse_code()
    for blob in (check, refuse):
        compile(blob, "<dify-code-node>", "exec")
    document = {
        "app": {
            "description": "Read one public document through the free reader and emit a checked rectangular CSV plus provenance. No model call.",
            "icon": "R",
            "icon_background": "#E0F2FE",
            "icon_type": "emoji",
            "mode": "workflow",
            "name": "Public document table CSV",
            "use_icon_as_answer_icon": False,
        },
        "dependencies": [],
        "kind": "app",
        "version": "0.7.0",
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [],
            "features": {
                "file_upload": {"enabled": False},
                "opening_statement": "",
                "retriever_resource": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False},
                "speech_to_text": {"enabled": False},
                "suggested_questions": [],
                "suggested_questions_after_answer": {"enabled": False},
                "text_to_speech": {"enabled": False, "language": "", "voice": ""},
            },
            "graph": {
                "edges": [
                    _edge("start", "gate", "start", "if-else"),
                    _edge("gate", "reader_http", "if-else", "http-request", "true"),
                    _edge("reader_http", "check_csv", "http-request", "code"),
                    _edge("check_csv", "end_checked", "code", "end"),
                    _edge("gate", "refuse_csv", "if-else", "code", "false"),
                    _edge("refuse_csv", "end_refused", "code", "end"),
                ],
                "nodes": [
                    _node(
                        "start",
                        {
                            "title": "Start",
                            "type": "start",
                            "variables": [
                                _start_variable("document_url", "Document URL", "text-input", 2048),
                                _start_variable("fields_json", "Requested fields JSON", "paragraph", 4000),
                                _start_variable("markdown_limit", "Markdown limit", "number", None),
                                _start_variable("page", "Page", "number", None),
                            ],
                        },
                        40,
                        220,
                        168,
                    ),
                    _node(
                        "gate",
                        {
                            "cases": [
                                {
                                    "case_id": "true",
                                    "conditions": [
                                        {
                                            "comparison_operator": "=",
                                            "id": "page-eq-1",
                                            "value": "1",
                                            "varType": "number",
                                            "variable_selector": ["start", "page"],
                                        }
                                    ],
                                    "id": "true",
                                    "logical_operator": "and",
                                }
                            ],
                            "desc": "The free reader has no page cursor. Only page 1 calls it.",
                            "title": "Page is 1",
                            "type": "if-else",
                        },
                        340,
                        220,
                        126,
                    ),
                    _node(
                        "reader_http",
                        {
                            "authorization": {"config": None, "type": "no-auth"},
                            "body": {
                                "data": [
                                    {
                                        "type": "text",
                                        "value": '{"url":"{{#start.document_url#}}","markdownLimitChars":{{#start.markdown_limit#}}}',
                                    }
                                ],
                                "type": "json",
                            },
                            "desc": "POST the disposable free-reader boundary. Automatic retry is off.",
                            "headers": "Content-Type: application/json",
                            "method": "post",
                            "params": "",
                            "retry_config": {
                                "max_retries": 0,
                                "retry_enabled": False,
                                "retry_interval": 0,
                            },
                            "ssl_verify": True,
                            "timeout": {"connect": 10, "read": 30, "write": 10},
                            "title": "Free reader",
                            "type": "http-request",
                            "url": READER_URL,
                        },
                        640,
                        80,
                        140,
                    ),
                    _node(
                        "check_csv",
                        {
                            "code": check,
                            "code_language": "python3",
                            "desc": "Select one rectangular table and write CSV plus provenance.",
                            "outputs": _string_outputs(),
                            "retry_config": {
                                "max_retries": 0,
                                "retry_enabled": False,
                                "retry_interval": 0,
                            },
                            "title": "Check CSV",
                            "type": "code",
                            "variables": [
                                {"value_selector": ["start", "document_url"], "variable": "document_url"},
                                {"value_selector": ["start", "fields_json"], "variable": "fields_json"},
                                {"value_selector": ["start", "markdown_limit"], "variable": "markdown_limit"},
                                {"value_selector": ["start", "page"], "variable": "page"},
                                {"value_selector": ["reader_http", "body"], "variable": "body"},
                                {"value_selector": ["reader_http", "status_code"], "variable": "status_code"},
                            ],
                        },
                        960,
                        80,
                        120,
                    ),
                    _node(
                        "refuse_csv",
                        {
                            "code": refuse,
                            "code_language": "python3",
                            "desc": "Page was not 1, so the reader was not called.",
                            "outputs": _string_outputs(),
                            "retry_config": {
                                "max_retries": 0,
                                "retry_enabled": False,
                                "retry_interval": 0,
                            },
                            "title": "Refuse page",
                            "type": "code",
                            "variables": [
                                {"value_selector": ["start", "document_url"], "variable": "document_url"},
                                {"value_selector": ["start", "fields_json"], "variable": "fields_json"},
                                {"value_selector": ["start", "markdown_limit"], "variable": "markdown_limit"},
                                {"value_selector": ["start", "page"], "variable": "page"},
                            ],
                        },
                        640,
                        380,
                        120,
                    ),
                    _node("end_checked", {"outputs": _end_outputs("check_csv"), "title": "Checked", "type": "end"}, 1280, 80, 160),
                    _node("end_refused", {"outputs": _end_outputs("refuse_csv"), "title": "Refused", "type": "end"}, 960, 380, 160),
                ],
                "viewport": {"x": 0, "y": 0, "zoom": 0.8},
            },
        },
    }
    kinds = [node["data"]["type"] for node in document["workflow"]["graph"]["nodes"]]
    found = FORBIDDEN.intersection(kinds)
    if found:
        raise SystemExit("forbidden node types: %s" % sorted(found))
    return document


def dump_document(document) -> str:
    class Dumper(yaml.SafeDumper):
        pass

    def represent_str(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    Dumper.add_representer(str, represent_str)
    return yaml.dump(document, Dumper=Dumper, allow_unicode=True, sort_keys=False, width=4096)


def main():
    destination = ROOT / "workflow" / "reader-table-csv.dsl.yml"
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = dump_document(build_document())
    destination.write_text(text)
    print(destination)


if __name__ == "__main__":
    main()
