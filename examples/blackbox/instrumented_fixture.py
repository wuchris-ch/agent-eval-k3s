"""Record a small instrumented fixture, not an AI agent or quality benchmark.

The trusted harness binds each final answer to its complete in-memory trace.
Only the input reaches answer(). No credentials or network calls are needed.
"""

import argparse
import json
import uuid
from pathlib import Path

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON

from agent_eval.blackbox.inspection_models import TraceRecord, TraceSpan
from agent_eval.blackbox.models import Observation, digest, json_bytes, load_suite
from agent_eval.paths import (
    atomic_write_private,
    ensure_private_directory,
    get_state_dir,
)


def search(value, tracer):
    with tracer.start_as_current_span(
        "search",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "search",
            "gen_ai.tool.call.arguments": json.dumps({"query": value}),
        },
    ) as span:
        result = (
            "Support is available Monday to Friday, 09:00 to 17:00 UTC."
            if value == "When is support available?"
            else "I don't have that information."
        )
        span.set_attribute("gen_ai.tool.call.result", result)
        return result


def validate(value, tracer):
    with tracer.start_as_current_span(
        "validate",
        attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "validate",
            "gen_ai.tool.call.arguments": json.dumps({"text": value}),
        },
    ) as span:
        result = isinstance(value, str) and bool(value.strip())
        span.set_attribute("gen_ai.tool.call.result", result)
        return result


def answer(value, tracer, *, wrong_order=False):
    if wrong_order:
        validate(value, tracer)
        return search(value, tracer)
    result = search(value, tracer)
    assert validate(result, tracer)
    return result


def record(wrong_order=False):
    suite = load_suite(Path(__file__).with_name("faq.yaml"))
    provider = TracerProvider(sampler=ALWAYS_ON)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("inspection-fixture")
    observations, records = [], []
    try:
        for case in suite.cases:
            exporter.clear()
            with tracer.start_as_current_span("request") as root:
                actual = answer(case.input, tracer, wrong_order=wrong_order)
            context = root.get_span_context()
            trace_id, root_id = f"{context.trace_id:032x}", f"{context.span_id:016x}"
            observations.append(
                Observation(
                    case_id=case.id,
                    input_sha256=digest(case.input),
                    actual_output=actual,
                    trace_id=trace_id,
                    span_id=root_id,
                )
            )
            spans = []
            for span in exporter.get_finished_spans():
                attrs = span.attributes
                tool = attrs.get("gen_ai.tool.name")
                spans.append(
                    TraceSpan(
                        span_id=f"{span.context.span_id:016x}",
                        parent_span_id=f"{span.parent.span_id:016x}"
                        if span.parent
                        else None,
                        name=span.name,
                        kind="tool" if tool else "agent",
                        status=span.status.status_code.name.lower(),
                        start_ns=span.start_time,
                        end_ns=span.end_time,
                        tool_name=tool,
                        arguments=attrs.get("gen_ai.tool.call.arguments"),
                        output=attrs.get("gen_ai.tool.call.result"),
                    )
                )
            records.append(
                TraceRecord(
                    case_id=case.id,
                    trial=1,
                    input_sha256=digest(case.input),
                    output_sha256=digest(actual),
                    trace_id=trace_id,
                    root_span_id=root_id,
                    # Synchronous in-memory collection, no sampling or dropped spans.
                    complete=True,
                    spans=spans,
                )
            )
    finally:
        provider.shutdown()
    directory = ensure_private_directory(
        get_state_dir() / "blackbox-fixtures" / str(uuid.uuid4()), parents=True
    )
    for filename, values in (
        ("observations.jsonl", observations),
        ("traces.jsonl", records),
    ):
        atomic_write_private(
            directory / filename,
            b"\n".join(json_bytes(value.model_dump(mode="json")) for value in values)
            + b"\n",
        )
    return directory


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrong-order", action="store_true")
    args = parser.parse_args()
    print(record(args.wrong_order))
