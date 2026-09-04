from __future__ import annotations

import json
import runpy
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_eval.blackbox.inspection import (
    InspectionEvidence,
    finish_evidence,
    inspect_report,
    prepare_inspection,
)
from agent_eval.blackbox.inspection_models import (
    InspectionCheck,
    InspectionProfile,
    SourceSelection,
    TraceRecord,
)
from agent_eval.blackbox.models import Case, Metric, Observation, Report, Suite, digest
from agent_eval.blackbox.runner import evaluate
from agent_eval.blackbox.source_evidence import collect_source
from agent_eval.blackbox.storage import save_report
from agent_eval.blackbox.telemetry import export_report
from agent_eval.blackbox.trace_evidence import load_traces
from agent_eval.cli import app


def baseline():
    suite = Suite(
        schema_version="1.0",
        id="generic-agent",
        version="1",
        metrics=[Metric(name="answer", kind="exact_match")],
        cases=[
            Case(id="question", input="Where is help?", expected_output="Help center")
        ],
    )
    observation = Observation(
        case_id="question",
        input_sha256=digest("Where is help?"),
        actual_output="Help center",
    )
    return suite, evaluate(suite, agent="any-agent", observations=[observation])


def profile(*checks, source=False):
    return InspectionProfile(
        schema_version="1.0",
        source=SourceSelection(include=["**/*.py"]) if source else None,
        checks=[InspectionCheck(**check) for check in checks],
    )


def trace_data(names=("search", "validate")):
    root = {
        "span_id": "1" * 16,
        "name": "agent",
        "kind": "agent",
        "start_ns": 0,
        "end_ns": 1000,
    }
    spans = [root]
    for index, name in enumerate(names, 2):
        spans.append(
            {
                "span_id": f"{index:016x}",
                "parent_span_id": root["span_id"],
                "name": name,
                "tool_name": name,
                "kind": "tool",
                "start_ns": index * 10,
                "end_ns": index * 10 + 5,
            }
        )
    return {
        "case_id": "question",
        "trial": 1,
        "input_sha256": digest("Where is help?"),
        "output_sha256": digest("Help center"),
        "complete": True,
        "trace_id": "a" * 32,
        "root_span_id": root["span_id"],
        "spans": spans,
    }


def evaluate_trace(check, data=None, required=False):
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(check),
        traces=[TraceRecord.model_validate(data or trace_data())],
    )
    return inspect_report(suite, report, evidence, required=required)


def test_collects_selected_source_without_executing_it(tmp_path):
    marker = tmp_path / "executed"
    (tmp_path / "agent.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    )
    (tmp_path / ".env").write_text("DO_NOT_COLLECT=example-value")
    (tmp_path / ".ENV.private").write_text("DO_NOT_COLLECT=example-value")
    (tmp_path / "auth.json").write_text('{"secret":"example"}')
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendor.py").write_text("vendor")
    (tmp_path / "skip.py").write_text("skip")
    snapshot = collect_source(
        tmp_path, SourceSelection(include=["**/*"], exclude=["skip.py"])
    )
    assert [file.path for file in snapshot.files] == ["agent.py"]
    assert not marker.exists()


@pytest.mark.parametrize(
    "pattern",
    ["../agent.py", "/absolute.py", "folder/../../a", "C:/a", "folder\\a", "a\nb"],
)
def test_source_paths_cannot_escape(pattern):
    with pytest.raises(ValidationError):
        SourceSelection(include=[pattern])


def test_source_links_and_limits_are_not_silently_accepted(tmp_path, monkeypatch):
    (tmp_path / "outside.txt").write_text("do not follow")
    (tmp_path / "link.py").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError):
        collect_source(tmp_path, SourceSelection(include=["*.py"]))
    (tmp_path / "link.py").unlink()
    (tmp_path / "large.py").write_text("longer than allowed")
    monkeypatch.setattr("agent_eval.blackbox.source_evidence.MAX_SOURCE_BYTES", 5)
    with pytest.raises(ValueError):
        collect_source(tmp_path, SourceSelection(include=["*.py"]))


def test_static_source_results_are_separate_from_output_and_trace(tmp_path):
    (tmp_path / "agent.py").write_text("def handle(): return 'Help center'\n")
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {
                "name": "known-handler",
                "kind": "source_contains",
                "path": "agent.py",
                "text": "def handle",
            },
            {
                "name": "avoid-return",
                "kind": "source_not_contains",
                "path": "agent.py",
                "text": "return",
            },
            source=True,
        ),
        source=collect_source(tmp_path, SourceSelection(include=["*.py"])),
    )
    inspected = inspect_report(suite, report, evidence, required=True)
    assert inspected.results == report.results
    assert inspected.passed and inspected.average_score == 1
    assert not inspected.overall_passed
    assert inspected.suite_sha256 == report.suite_sha256
    assert inspected.inspection.source.rejected == 1
    assert inspected.inspection.trace.status == "not_requested"
    assert inspect_report(suite, report, evidence).overall_passed


def test_missing_source_file_cannot_pass_a_negative_check(tmp_path):
    (tmp_path / "other.py").write_text("other")
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {
                "name": "no-secret",
                "kind": "source_not_contains",
                "path": "missing.py",
                "text": "secret",
            },
            source=True,
        ),
        source=collect_source(tmp_path, SourceSelection(include=["*.py"])),
    )
    result = inspect_report(suite, report, evidence).inspection.results[0]
    assert result.status == "unavailable" and result.error == "source_path_missing"


def test_source_changes_during_execution_are_reported(tmp_path):
    source = tmp_path / "agent.py"
    source.write_text("before")
    config = profile(
        {
            "name": "check",
            "kind": "source_contains",
            "path": "agent.py",
            "text": "before",
        },
        source=True,
    )
    path = tmp_path / "profile.json"
    path.write_text(config.model_dump_json())
    evidence = prepare_inspection(path, tmp_path)
    source.write_text("after")
    finish_evidence(evidence, tmp_path, None)
    suite, report = baseline()
    inspected = inspect_report(suite, report, evidence, required=True)
    assert inspected.inspection.results[0].error == "source_changed"
    assert evidence.source.files[0].content == "before"
    assert inspected.passed and not inspected.overall_passed


@pytest.mark.parametrize(
    ("check", "passed"),
    [
        ({"name": "required", "kind": "tools_required", "tools": ["search"]}, True),
        ({"name": "required", "kind": "tools_required", "tools": ["unknown"]}, False),
        ({"name": "forbidden", "kind": "tools_forbidden", "tools": ["delete"]}, True),
        ({"name": "forbidden", "kind": "tools_forbidden", "tools": ["search"]}, False),
        ({"name": "budget", "kind": "tool_budget", "max_calls": 2}, True),
        ({"name": "budget", "kind": "tool_budget", "max_calls": 1}, False),
        (
            {"name": "order", "kind": "tool_sequence", "tools": ["search", "validate"]},
            True,
        ),
        (
            {"name": "order", "kind": "tool_sequence", "tools": ["validate", "search"]},
            False,
        ),
        (
            {"name": "repeat", "kind": "tool_sequence", "tools": ["search", "search"]},
            False,
        ),
        ({"name": "errors", "kind": "trace_no_errors"}, True),
    ],
)
def test_tool_checks(check, passed):
    result = evaluate_trace(check, required=True)
    assert result.inspection.trace.status == ("accepted" if passed else "rejected")
    assert result.passed and result.overall_passed is passed


def test_sequence_requires_completion_before_next_call():
    data = trace_data()
    data["spans"][1]["end_ns"] = 100
    result = evaluate_trace(
        {"name": "order", "kind": "tool_sequence", "tools": ["search", "validate"]},
        data,
    )
    assert result.inspection.trace.rejected == 1


def test_tool_errors_count_as_calls_and_are_visible():
    data = trace_data()
    data["spans"][1]["status"] = "error"
    assert (
        evaluate_trace(
            {"name": "no-errors", "kind": "trace_no_errors"}, data
        ).inspection.trace.rejected
        == 1
    )
    assert (
        evaluate_trace(
            {"name": "budget", "kind": "tool_budget", "max_calls": 1}, data
        ).inspection.trace.rejected
        == 1
    )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("input_sha256", "0" * 64, "trace_binding_mismatch"),
        ("output_sha256", "0" * 64, "trace_binding_mismatch"),
        ("complete", False, "trace_incomplete"),
    ],
)
def test_trace_binding_and_completeness(field, value, error):
    data = trace_data()
    data[field] = value
    result = evaluate_trace(
        {"name": "no-delete", "kind": "tools_forbidden", "tools": ["delete"]},
        data,
        required=True,
    )
    assert result.inspection.results[0].error == error
    assert result.inspection.trace.score is None
    assert result.passed and not result.overall_passed


def test_trace_identity_must_match_linked_observation():
    suite, report = baseline()
    report.results[0].observation.trace_id = "b" * 32
    report.results[0].observation.span_id = "1" * 16
    evidence = InspectionEvidence(
        profile=profile({"name": "errors", "kind": "trace_no_errors"}),
        traces=[TraceRecord.model_validate(trace_data())],
    )
    assert (
        inspect_report(suite, report, evidence).inspection.results[0].error
        == "trace_identity_mismatch"
    )


@pytest.mark.parametrize(
    "defect",
    [
        "missing-parent",
        "cycle",
        "duplicate",
        "missing-root",
        "missing-complete",
        "backwards-time",
    ],
)
def test_invalid_trace_structure(defect):
    data = trace_data()
    if defect == "missing-parent":
        data["spans"][1]["parent_span_id"] = "9" * 16
    elif defect == "cycle":
        data["spans"][1]["parent_span_id"] = data["spans"][2]["span_id"]
        data["spans"][2]["parent_span_id"] = data["spans"][1]["span_id"]
    elif defect == "duplicate":
        data["spans"].append(data["spans"][1])
    elif defect == "missing-root":
        data["root_span_id"] = "9" * 16
    elif defect == "missing-complete":
        del data["complete"]
    else:
        data["spans"][1]["end_ns"] = 0
    with pytest.raises(ValidationError):
        TraceRecord.model_validate(data)


def test_trace_loader_rejects_duplicate_trials_and_ambiguous_json(tmp_path):
    path = tmp_path / "traces.jsonl"
    line = json.dumps(trace_data())
    path.write_text(line + "\n" + line)
    with pytest.raises(ValueError):
        load_traces(path)
    path.write_text('{"case_id":"one","case_id":"two"}')
    with pytest.raises(ValueError):
        load_traces(path)


def test_missing_or_invalid_trace_does_not_change_baseline(tmp_path):
    suite, report = baseline()
    for content in (None, "private-malformed-trace"):
        evidence = InspectionEvidence(
            profile=profile({"name": "errors", "kind": "trace_no_errors"})
        )
        path = tmp_path / "trace.json"
        if content:
            path.write_text(content)
        finish_evidence(evidence, None, path)
        result = inspect_report(suite, report, evidence)
        assert result.passed and result.overall_passed
        assert result.inspection.trace.status == "unavailable"
        assert "private-malformed-trace" not in result.model_dump_json()


def otlp(data):
    bindings = {
        "agent_eval.case.id": data["case_id"],
        "agent_eval.trial.number": data["trial"],
        "agent_eval.input.sha256": data["input_sha256"],
        "agent_eval.output.sha256": data["output_sha256"],
        "agent_eval.trace.complete": data["complete"],
    }

    def attrs(values):
        return [
            {
                "key": key,
                "value": {
                    "boolValue"
                    if type(value) is bool
                    else "intValue"
                    if type(value) is int
                    else "stringValue": value
                },
            }
            for key, value in values.items()
        ]

    spans = []
    for span in data["spans"]:
        values = (
            bindings
            if span["span_id"] == data["root_span_id"]
            else {
                "gen_ai.tool.name": span["tool_name"],
                "gen_ai.tool.call.arguments": '{"query":"help"}',
                "private.extra": "omit-this",
            }
        )
        spans.append(
            {
                "traceId": data["trace_id"].upper(),
                "spanId": span["span_id"],
                "parentSpanId": span.get("parent_span_id", ""),
                "name": span["name"],
                "startTimeUnixNano": str(span["start_ns"] + 1),
                "endTimeUnixNano": str(span["end_ns"] + 1),
                "attributes": attrs(values),
                "status": {"code": 0},
            }
        )
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def test_otlp_import_preserves_tools_and_excludes_unneeded_attributes(tmp_path):
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(otlp(trace_data())))
    traces = load_traces(path)
    assert traces[0].trace_id == "a" * 32
    assert traces[0].spans[1].tool_name == "search"
    assert traces[0].spans[1].arguments == '{"query":"help"}'
    assert "omit-this" not in traces[0].model_dump_json()
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {"name": "order", "kind": "tool_sequence", "tools": ["search", "validate"]}
        ),
        traces=traces,
    )
    assert inspect_report(suite, report, evidence, required=True).overall_passed


def test_otlp_structured_arguments_and_error_type(tmp_path):
    payload = otlp(trace_data())
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    attributes = spans[1]["attributes"]
    attributes[1]["value"] = {
        "kvlistValue": {
            "values": [
                {"key": "query", "value": {"stringValue": "help"}},
                {
                    "key": "ids",
                    "value": {
                        "arrayValue": {
                            "values": [{"intValue": "4"}, {"boolValue": True}]
                        }
                    },
                },
            ]
        }
    }
    attributes.append({"key": "error.type", "value": {"stringValue": "TimeoutError"}})
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(payload))
    record = load_traces(path)[0]
    assert record.spans[1].arguments == {"query": "help", "ids": [4, True]}
    assert record.spans[1].status == "error"


@pytest.mark.parametrize(
    "defect",
    [
        "orphan",
        "disconnected",
        "duplicate",
        "cycle",
        "unnamed-tool",
        "float-time",
        "bool-time",
        "string-status",
    ],
)
def test_otlp_rejects_exports_that_could_hide_calls(tmp_path, defect):
    payload = otlp(trace_data())
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    if defect == "orphan":
        spans[1]["parentSpanId"] = "9" * 16
    elif defect == "disconnected":
        spans[1]["parentSpanId"] = ""
    elif defect == "duplicate":
        spans.append(spans[1])
    elif defect == "cycle":
        spans[1]["parentSpanId"] = spans[2]["spanId"]
        spans[2]["parentSpanId"] = spans[1]["spanId"]
    elif defect == "unnamed-tool":
        spans[1]["attributes"] = [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}}
        ]
    elif defect in {"float-time", "bool-time"}:
        spans[1]["startTimeUnixNano"] = 1.5 if defect == "float-time" else True
    else:
        spans[1]["status"]["code"] = "STATUS_CODE_OK"
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        load_traces(path)


def test_otlp_subtree_allows_external_parent_and_marks_dropped_attributes(tmp_path):
    payload = otlp(trace_data())
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans[0]["parentSpanId"] = "9" * 16
    spans[1]["droppedAttributesCount"] = 1
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(payload))
    record = load_traces(path)[0]
    assert record.spans[0].parent_span_id is None
    assert not record.complete


def test_otlp_subtree_excludes_connected_server_siblings(tmp_path):
    payload = otlp(trace_data())
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans[0]["parentSpanId"] = "9" * 16
    server = dict(spans[0], spanId="9" * 16, parentSpanId="", attributes=[])
    sibling = dict(spans[0], spanId="8" * 16, parentSpanId="9" * 16, attributes=[])
    spans.extend([server, sibling])
    path = tmp_path / "otlp.json"
    path.write_text(json.dumps(payload))
    record = load_traces(path)[0]
    assert len(record.spans) == 3 and record.complete


def test_geval_inspection_gets_internal_context_without_changing_output_check(tmp_path):
    (tmp_path / "agent.py").write_text("SOURCE_ONLY = 'source evidence'")
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {
                "name": "source-quality",
                "kind": "source_geval",
                "criteria": "Assess the implementation's support for the answer.",
                "threshold": 0.6,
            },
            {
                "name": "trace-quality",
                "kind": "trace_geval",
                "criteria": "Check that tools support the answer.",
                "threshold": 0.6,
            },
            source=True,
        ),
        source=collect_source(tmp_path, SourceSelection(include=["*.py"])),
        traces=[TraceRecord.model_validate(trace_data())],
    )
    calls = []

    def measure(metric, case, actual):
        calls.append((metric, case, actual))
        return 0.2, "inspection failed"

    result = inspect_report(
        suite, report, evidence, judge=SimpleNamespace(measure=measure), required=True
    )
    assert result.average_score == 1 and result.passed and not result.overall_passed
    assert "SOURCE_ONLY" in calls[0][1].context[-1]
    assert "execution_trace" in calls[1][1].context[-1]
    assert suite.cases[0].context == []
    assert result.results == report.results


def test_judge_failure_is_unavailable_not_a_failed_output():
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {"name": "judge", "kind": "trace_geval", "criteria": "Check tools."}
        ),
        traces=[TraceRecord.model_validate(trace_data())],
    )
    result = inspect_report(
        suite,
        report,
        evidence,
        judge=SimpleNamespace(measure=lambda *_: (float("nan"), "private")),
        required=True,
    )
    assert result.inspection.results[0].error == "judge_error"
    assert result.passed and not result.overall_passed


def test_profile_rejects_ignored_parameters():
    with pytest.raises(ValidationError):
        profile({"name": "bad", "kind": "trace_no_errors", "tools": ["ignored"]})
    with pytest.raises(ValidationError):
        profile(
            {"name": "bad", "kind": "tool_budget", "max_calls": 1, "threshold": 0.5}
        )


def test_saved_inspection_preserves_original_run_and_private_snapshots(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_EVAL_STATE_DIR", str(tmp_path / "state"))
    suite, original = baseline()
    original_path = save_report(suite, original)
    before = original_path.read_bytes()
    (tmp_path / "agent.py").write_text("def handle(): return 'Help center'")
    config = profile(
        {
            "name": "check",
            "kind": "source_contains",
            "path": "agent.py",
            "text": "def handle",
        },
        source=True,
    )
    profile_path = tmp_path / "inspection.json"
    profile_path.write_text(config.model_dump_json())
    response = CliRunner().invoke(
        app,
        [
            "blackbox",
            "inspect",
            "--report",
            str(original_path),
            "--inspection",
            str(profile_path),
            "--source-root",
            str(tmp_path),
            "--require-inspection",
        ],
    )
    assert response.exit_code == 0, response.output
    assert original_path.read_bytes() == before
    new_path = next(
        path
        for path in (tmp_path / "state" / "blackbox").glob("*/report.json")
        if path != original_path
    )
    new_report = Report.model_validate_json(new_path.read_bytes())
    assert new_report.parent_run_id == original.run_id and new_report.overall_passed
    assert new_report.results == original.results
    assert (new_path.parent / "source.json").stat().st_mode & 0o777 == 0o600
    assert "def handle" not in new_path.read_text()
    with sqlite3.connect(new_path.parent.parent / "metrics.db") as connection:
        assert connection.execute(
            "SELECT source_status, overall_passed FROM inspection_runs"
        ).fetchone() == ("accepted", 1)


@pytest.mark.parametrize("wrong_order", [False, True])
def test_instrumented_example_through_cli(tmp_path, monkeypatch, wrong_order):
    pytest.importorskip("opentelemetry.sdk")
    monkeypatch.setenv("AGENT_EVAL_STATE_DIR", str(tmp_path / "state"))
    examples = Path(__file__).resolve().parents[1] / "examples" / "blackbox"
    fixture = runpy.run_path(str(examples / "instrumented_fixture.py"))
    directory = fixture["record"](wrong_order=wrong_order)
    result = CliRunner().invoke(
        app,
        [
            "blackbox",
            "replay",
            "--suite",
            str(examples / "faq.yaml"),
            "--agent",
            "instrumented-fixture",
            "--observations",
            str(directory / "observations.jsonl"),
            "--inspection",
            str(examples / "inspection.yaml"),
            "--source-root",
            str(examples),
            "--traces",
            str(directory / "traces.jsonl"),
            "--require-inspection",
        ],
    )
    assert result.exit_code == (2 if wrong_order else 0), result.output
    path = next((tmp_path / "state" / "blackbox").glob("*/report.json"))
    report = Report.model_validate_json(path.read_bytes())
    assert report.passed and report.average_score == 1
    assert report.overall_passed is not wrong_order
    assert report.inspection.source.status == "accepted"
    failed = [item for item in report.inspection.results if item.status == "rejected"]
    assert len(failed) == (2 if wrong_order else 0)
    assert all(item.kind == "tool_sequence" for item in failed)


def test_source_snapshot_cannot_be_changed_after_inspection(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_EVAL_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "agent.py").write_text("original")
    suite, report = baseline()
    evidence = InspectionEvidence(
        profile=profile(
            {
                "name": "source",
                "kind": "source_contains",
                "path": "agent.py",
                "text": "original",
            },
            source=True,
        ),
        source=collect_source(tmp_path, SourceSelection(include=["*.py"])),
    )
    inspected = inspect_report(suite, report, evidence)
    evidence.source.files[0].content = "modified"
    with pytest.raises(ValueError):
        save_report(suite, inspected, evidence=evidence)


@pytest.mark.parametrize("required", [False, True])
def test_cli_missing_inspection_evidence_has_explicit_gate(
    tmp_path, monkeypatch, required
):
    monkeypatch.setenv("AGENT_EVAL_STATE_DIR", str(tmp_path / "state"))
    suite, report = baseline()
    path = save_report(suite, report)
    config = tmp_path / "profile.json"
    config.write_text(
        profile({"name": "trace", "kind": "trace_no_errors"}).model_dump_json()
    )
    args = ["blackbox", "inspect", "--report", str(path), "--inspection", str(config)]
    if required:
        args += ["--require-inspection"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == (2 if required else 0), result.output
    assert "trace_missing" in result.output and "unavailable" in result.output


def test_inspection_telemetry_does_not_export_raw_internal_content(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setenv("AGENT_EVAL_OTEL_ENABLED", "1")
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    monkeypatch.setattr(
        "agent_eval.observability._get_runtime",
        lambda: SimpleNamespace(provider=provider, tracer=provider.get_tracer("test")),
    )
    result = evaluate_trace(
        {"name": "private-check-name", "kind": "tools_required", "tools": ["search"]}
    )
    assert export_report(result)
    spans = exporter.get_finished_spans()
    internal = next(span for span in spans if span.name == "agent_eval.inspection")
    assert internal.attributes["agent_eval.inspection.status"] == "accepted"
    contents = str(
        [
            (dict(span.attributes), [dict(event.attributes) for event in span.events])
            for span in spans
        ]
    )
    assert "private-check-name" not in contents and "Help center" not in contents
    provider.shutdown()
