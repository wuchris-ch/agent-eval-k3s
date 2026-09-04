"""Normalize complete execution traces from JSONL or OTLP JSON exports."""

from __future__ import annotations

import math
import re
from pathlib import Path

from ..limits import MAX_RESULTS_JSON_BYTES, read_stable_bounded_file
from .inspection_models import TraceRecord, TraceSpan
from .models import MAX_EVALUATIONS, json_bytes, parse_json

MAX_TRACE_BYTES = 512 * 1024
MAX_TRACE_SPANS = 20_000
ATTRIBUTES = {
    "agent_eval.case.id",
    "agent_eval.trial.number",
    "agent_eval.input.sha256",
    "agent_eval.output.sha256",
    "agent_eval.trace.complete",
    "gen_ai.tool.name",
    "gen_ai.operation.name",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "error.type",
}


def _integer(value) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    raise ValueError("invalid OTLP integer")


def _value(value: dict, depth: int = 0):
    if depth > 20 or not isinstance(value, dict) or len(value) > 1:
        raise ValueError("invalid OTLP attribute value")
    if not value:
        return None
    kind, actual = next(iter(value.items()))
    if kind == "intValue":
        return _integer(actual)
    if kind in {"stringValue", "bytesValue"} and isinstance(actual, str):
        return actual
    if kind == "boolValue" and type(actual) is bool:
        return actual
    if kind == "doubleValue" and type(actual) in {int, float} and math.isfinite(actual):
        return actual
    if kind == "arrayValue":
        return [_value(item, depth + 1) for item in actual.get("values", [])]
    if kind == "kvlistValue":
        result = {}
        for item in actual.get("values", []):
            key = item["key"]
            if not isinstance(key, str) or key in result:
                raise ValueError("invalid OTLP object key")
            result[key] = _value(item["value"], depth + 1)
        return result
    raise ValueError("invalid OTLP attribute value")


def _attributes(items: list) -> dict:
    result = {}
    seen = set()
    for item in items:
        key, value = item["key"], item["value"]
        if key in seen:
            raise ValueError("duplicate trace attribute")
        seen.add(key)
        if key in ATTRIBUTES:
            result[key] = _value(value)
    return result


def _subtree(raw_spans: list[dict], root_id: str) -> set[str]:
    by_id = {span["spanId"].lower(): span for span in raw_spans}
    if len(by_id) != len(raw_spans):
        raise ValueError("duplicate OTLP span IDs")
    children = {}
    for span_id, span in by_id.items():
        parent = span.get("parentSpanId", "").lower()
        if parent and parent not in by_id and span_id != root_id:
            raise ValueError("OTLP trace has a missing parent")
        children.setdefault(parent, []).append(span_id)
    anchors = {}
    for span_id in by_id:
        current, visited = span_id, set()
        while current not in anchors:
            if current in visited:
                raise ValueError("OTLP trace is cyclic")
            visited.add(current)
            parent = by_id[current].get("parentSpanId", "").lower()
            if parent not in by_id:
                anchor = current
                break
            current = parent
        else:
            anchor = anchors[current]
        anchors.update(dict.fromkeys(visited, anchor))
    if len(set(anchors.values())) != 1:
        raise ValueError("OTLP trace contains disconnected spans")
    descendants, pending = set(), [root_id]
    while pending:
        current = pending.pop()
        descendants.add(current)
        if len(descendants) > 2000:
            raise ValueError("individual trace exceeds the span limit")
        pending.extend(children.get(current, []))
    return descendants


def _otlp_records(payload: dict) -> list[TraceRecord]:
    grouped = {}
    count = 0
    for resource in payload["resourceSpans"]:
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                count += 1
                if count > MAX_TRACE_SPANS:
                    raise ValueError("trace export exceeds the span limit")
                grouped.setdefault(span["traceId"].lower(), []).append(span)
    records = []
    for trace_id, raw_spans in grouped.items():
        # Exactly one explicitly bound evaluation root per trace. Unbound traces
        # are ignored, never guessed from timestamps or the first available span.
        roots = [
            span
            for span in raw_spans
            if "agent_eval.case.id" in _attributes(span.get("attributes", []))
        ]
        if not roots:
            continue
        if len(roots) != 1:
            raise ValueError("OTLP trace has ambiguous evaluation roots")
        root = roots[0]
        root_id = root["spanId"].lower()
        attrs = _attributes(root.get("attributes", []))
        # Select just this root's subtree, excluding surrounding server spans.
        # Validate the graph first so orphaned tool calls cannot disappear.
        descendants = _subtree(raw_spans, root_id)
        spans = []
        complete = attrs.get("agent_eval.trace.complete", False)
        for span in raw_spans:
            span_id = span["spanId"].lower()
            if span_id not in descendants:
                continue
            values = _attributes(span.get("attributes", []))
            tool = values.get("gen_ai.tool.name")
            operation = values.get("gen_ai.operation.name")
            if operation == "execute_tool" and not tool:
                raise ValueError("OTLP tool call is missing its tool name")
            kind = (
                "tool"
                if tool
                else (
                    "agent"
                    if span_id == root_id
                    else "model"
                    if operation in {"chat", "text_completion", "generate_content"}
                    else "internal"
                )
            )
            status = span.get("status", {}).get("code", 0)
            if type(status) is not int or status not in {0, 1, 2}:
                raise ValueError("invalid OTLP status code")
            if values.get("error.type"):
                status = 2
            if _integer(span.get("endTimeUnixNano", 0)) <= 0:
                raise ValueError("OTLP span has not ended")
            dropped = _integer(span.get("droppedAttributesCount", 0))
            if dropped < 0:
                raise ValueError("invalid OTLP dropped attribute count")
            if dropped:
                complete = False
            spans.append(
                TraceSpan(
                    span_id=span_id,
                    parent_span_id=None
                    if span_id == root_id
                    else span.get("parentSpanId", "").lower() or None,
                    name=span["name"],
                    kind=kind,
                    status={0: "unset", 1: "ok", 2: "error"}[status],
                    start_ns=_integer(span["startTimeUnixNano"]),
                    end_ns=_integer(span["endTimeUnixNano"]),
                    tool_name=tool,
                    arguments=values.get("gen_ai.tool.call.arguments"),
                    output=values.get("gen_ai.tool.call.result"),
                )
            )
        records.append(
            TraceRecord(
                case_id=attrs["agent_eval.case.id"],
                trial=attrs["agent_eval.trial.number"],
                input_sha256=attrs["agent_eval.input.sha256"],
                output_sha256=attrs["agent_eval.output.sha256"],
                trace_id=trace_id,
                root_span_id=root_id,
                complete=complete,
                spans=spans,
            )
        )
    return records


def load_traces(path: Path) -> list[TraceRecord]:
    raw = read_stable_bounded_file(path, maximum_bytes=MAX_RESULTS_JSON_BYTES)
    try:
        payload = parse_json(raw)
    except ValueError:
        # Multiple complete JSON objects are the normalized JSONL format.
        payload = [parse_json(line) for line in raw.splitlines() if line.strip()]
    if isinstance(payload, dict) and "resourceSpans" in payload:
        records = _otlp_records(payload)
    else:
        records = [
            TraceRecord.model_validate(value)
            for value in (payload if isinstance(payload, list) else [payload])
        ]
    if (
        len(records) > MAX_EVALUATIONS
        or sum(len(record.spans) for record in records) > MAX_TRACE_SPANS
    ):
        raise ValueError("trace export exceeds the record or span limit")
    keys = [(record.case_id, record.trial) for record in records]
    identities = [(record.trace_id, record.root_span_id) for record in records]
    if len(keys) != len(set(keys)) or len(identities) != len(set(identities)):
        raise ValueError("trace export contains duplicate bindings")
    for record in records:
        if len(json_bytes(record.model_dump(mode="json"))) > MAX_TRACE_BYTES:
            raise ValueError("individual trace exceeds the content limit")
        record.spans.sort(key=lambda span: (span.start_ns, span.end_ns, span.span_id))
    return sorted(records, key=lambda record: (record.case_id, record.trial))
