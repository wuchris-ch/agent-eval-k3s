from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_eval.audit import (
    AUDIT_SCHEMA_VERSION,
    GENESIS_HASH,
    MAX_AUDIT_EVENT_BYTES,
    AuditChain,
    AuditError,
    AuditEvent,
    canonical_audit_json_bytes,
    verify_audit_chain,
)

TRACE_ID = "a" * 32
FIRST_SPAN_ID = "b" * 16
SECOND_SPAN_ID = "c" * 16
TIMESTAMP = "2026-07-14T12:34:56.123456Z"

PRODUCTION_EVENT_ATTRIBUTES = [
    (
        "evaluation.requested",
        {
            "request_id": "d3b07384-d9a0-4f03-9f75-12e0a724b46c",
            "task_id": "example-task",
            "agent": "claude-code",
            "model": "claude-sonnet-4-5-20250929",
            "trial": 1,
            "run_scans": True,
            "run_judge": True,
            "judge_backend": "claude",
            "judge_model": "claude-sonnet-4-5-20250929",
            "task_tree_sha256": "1" * 64,
            "execution_spec_digest": "2" * 64,
            "task_image_digest": "sha256:" + "6" * 64,
            "task_image_ref": "agent-eval/example-task:governed-" + "6" * 64,
            "task_image_platform": "linux/amd64",
        },
    ),
    (
        "policy.admitted",
        {
            "decision_id": "6247e8d5-03d6-4e08-b69b-b66797857c11",
            "request_digest": "3" * 64,
            "policy_id": "enterprise-default",
            "policy_revision": "2026-07-14",
            "policy_digest": "4" * 64,
            "task_registry_id": "approved-tasks",
            "task_registry_revision": "2026-07-14",
            "task_registry_digest": "7" * 64,
            "registry_id": "approved-models",
            "registry_revision": "2026-07-14",
            "registry_digest": "5" * 64,
        },
    ),
    (
        "agent.started",
        {
            "agent": "claude-code",
            "model": "claude-sonnet-4-5-20250929",
            "trial": 1,
        },
    ),
    (
        "agent.completed",
        {
            "status": "completed",
            "exit_code": 0,
            "timed_out": False,
            "snapshot_available": True,
            "wall_time_s": 12.3,
            "total_tokens": 42,
        },
    ),
    ("cleanup.completed", {"status": "completed", "failure_count": 0}),
    ("evaluation.started", {"task_id": "example-task", "trial": 1}),
    ("evaluation.failed", {"exception_type": "RuntimeError"}),
    (
        "tests.completed",
        {
            "status": "completed",
            "resolved": True,
            "passed": 12,
            "total": 12,
            "command_exit_code": 0,
        },
    ),
    (
        "scanners.completed",
        {"status": "completed", "finding_count": 0, "scanner_count": 3},
    ),
    (
        "judge.completed",
        {
            "status": "completed",
            "score_available": True,
            "dimension_count": 4,
            "backend": "claude",
            "model": "claude-sonnet-4-5-20250929",
        },
    ),
    ("judge.skipped", {"reason_code": "disabled"}),
    (
        "outcome.decided",
        {"status": "accepted", "check_count": 8, "reason_count": 0},
    ),
    ("run.completed", {"status": "accepted"}),
]


def _event_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "sequence": 0,
        "timestamp": TIMESTAMP,
        "trace_id": TRACE_ID,
        "span_id": FIRST_SPAN_ID,
        "parent_span_id": None,
        "run_id": "run-1",
        "event_type": "agent.started",
        "attributes": {"agent": "claude-code", "model": "model-1", "trial": 1},
        "previous_hash": GENESIS_HASH,
        "event_hash": "d" * 64,
    }
    data.update(overrides)
    return data


def _make_chain(path: Path) -> tuple[list[dict], str]:
    with AuditChain(path, "run-1", trace_id=TRACE_ID) as chain:
        first = chain.append(
            "agent.started",
            {"agent": "claude-code", "model": "model-1", "trial": 1},
            span_id=FIRST_SPAN_ID,
            timestamp=TIMESTAMP,
        )
        second = chain.append(
            "agent.completed",
            {
                "status": "completed",
                "exit_code": 0,
                "timed_out": False,
                "snapshot_available": True,
                "wall_time_s": 5.0,
                "total_tokens": 12,
            },
            span_id=SECOND_SPAN_ID,
            parent_span_id=FIRST_SPAN_ID,
            timestamp="2026-07-14T12:35:01+00:00",
        )
        final_hash = chain.final_hash
    assert final_hash is not None
    return [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ], final_hash


def _read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rehash(event: dict) -> None:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    event["event_hash"] = hashlib.sha256(
        canonical_audit_json_bytes(payload)
    ).hexdigest()


def _write_events(path: Path, events: list[dict]) -> None:
    path.write_bytes(
        b"".join(canonical_audit_json_bytes(event) + b"\n" for event in events)
    )


def _codes(result) -> set[str]:
    return {failure.code for failure in result.failures}


def test_audit_event_is_strict_and_forbids_extra_fields():
    event = AuditEvent.model_validate(_event_data())
    assert event.schema_version == AUDIT_SCHEMA_VERSION
    assert event.sequence == 0

    with pytest.raises(ValidationError):
        AuditEvent.model_validate(_event_data(sequence="0"))
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(_event_data(unexpected=True))
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(_event_data(schema_version="agent-eval.audit/v2"))


@pytest.mark.parametrize(("event_type", "attributes"), PRODUCTION_EVENT_ATTRIBUTES)
def test_production_event_attribute_contract(event_type, attributes):
    event = AuditEvent.model_validate(
        _event_data(event_type=event_type, attributes=attributes)
    )

    assert event.event_type == event_type
    assert event.attributes == attributes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timestamp", "2026-07-14T12:34:56"),
        ("timestamp", "2026-02-30T12:34:56Z"),
        ("trace_id", "A" * 32),
        ("trace_id", "a" * 31),
        ("span_id", "g" * 16),
        ("parent_span_id", "a" * 15),
        ("previous_hash", "A" * 64),
        ("event_hash", "0" * 63),
    ],
)
def test_audit_event_rejects_invalid_timestamp_and_identifiers(field, value):
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(_event_data(**{field: value}))


@pytest.mark.parametrize(
    "attributes",
    [
        {"Authorization": "Bearer token"},
        {"API-Key": "token"},
        {"apiKey": "token"},
        {"safe": [{"Pass Word": "token"}]},
        {"safe": {"ＣＯＯＫＩＥ": "token"}},
        {"nested": {"StdOut": "private output"}},
        {"completion": "private output"},
    ],
)
def test_sensitive_attribute_keys_are_rejected_recursively(attributes):
    with pytest.raises(ValidationError, match="forbidden sensitive key"):
        AuditEvent.model_validate(_event_data(attributes=attributes))


def test_allowlisted_non_content_metrics_are_allowed():
    event = AuditEvent.model_validate(
        _event_data(
            event_type="agent.completed",
            attributes={
                "status": "completed",
                "exit_code": 0,
                "timed_out": False,
                "snapshot_available": True,
                "wall_time_s": 1.5,
                "total_tokens": 42,
            },
        )
    )
    assert event.attributes["total_tokens"] == 42


@pytest.mark.parametrize(
    ("event_type", "attributes", "field"),
    [
        (
            "agent.completed",
            {
                "status": "completed",
                "exit_code": -9,
                "timed_out": False,
                "snapshot_available": True,
                "wall_time_s": 1.5,
                "total_tokens": 42,
            },
            "exit_code",
        ),
        (
            "tests.completed",
            {
                "status": "infrastructure_error",
                "resolved": False,
                "passed": 0,
                "total": 0,
                "command_exit_code": -15,
            },
            "command_exit_code",
        ),
    ],
)
def test_signed_subprocess_exit_codes_are_allowed(event_type, attributes, field):
    event = AuditEvent.model_validate(
        _event_data(event_type=event_type, attributes=attributes)
    )

    assert event.attributes[field] < 0


@pytest.mark.parametrize(
    ("event_type", "attributes"),
    [
        ("run.started", {}),
        ("agent.started", {"system_prompt": "private instructions"}),
        ("agent.completed", {"response_body": "private response"}),
        (
            "agent.started",
            {
                "agent": "claude-code",
                "model": {"system_prompt": "TOP SECRET"},
                "trial": 1,
            },
        ),
    ],
)
def test_chain_rejects_unknown_event_types_and_attributes_before_append(
    tmp_path, event_type, attributes
):
    path = tmp_path / "audit.jsonl"
    with AuditChain(path, "run-1", trace_id=TRACE_ID) as chain:
        with pytest.raises(AuditError, match="strict schema"):
            chain.append(event_type, attributes, timestamp=TIMESTAMP)

        assert chain.event_count == 0
        assert chain.final_hash is None
        assert path.read_bytes() == b""


@pytest.mark.parametrize(
    ("event_type", "attributes"),
    [
        ("agent.started", {"agent": "claude-code", "trial": 1}),
        (
            "agent.started",
            {"agent": "claude-code", "model": "model-1", "trial": True},
        ),
        (
            "agent.completed",
            {
                "status": "completed",
                "exit_code": 0,
                "timed_out": False,
                "snapshot_available": True,
                "wall_time_s": 1.0,
                "total_tokens": -1,
            },
        ),
        (
            "evaluation.requested",
            {"task_id": "example-task"},
        ),
        (
            "evaluation.requested",
            {
                "task_id": "example-task",
                "task_image_digest": "sha256:" + "A" * 64,
            },
        ),
        (
            "evaluation.failed",
            {"exception_type": "x" * 129},
        ),
        (
            "scanners.completed",
            {"status": "skipped", "finding_count": 0, "scanner_count": 0},
        ),
    ],
)
def test_chain_rejects_missing_mistyped_negative_and_branch_invalid_attributes(
    tmp_path, event_type, attributes
):
    path = tmp_path / "audit.jsonl"
    with AuditChain(path, "run-1", trace_id=TRACE_ID) as chain:
        with pytest.raises(AuditError, match="strict schema"):
            chain.append(event_type, attributes, timestamp=TIMESTAMP)

        assert chain.event_count == 0
        assert chain.final_hash is None
        assert path.read_bytes() == b""


@pytest.mark.parametrize(
    "attributes",
    [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": (1, 2)},
        {1: "non-string key"},
        {"value": b"bytes"},
    ],
)
def test_attributes_must_be_strict_finite_json(attributes):
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(_event_data(attributes=attributes))


def test_chain_writes_canonical_durable_0600_jsonl(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    expected, final_hash = _make_chain(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(fsync_calls) == 2
    assert path.read_bytes().endswith(b"\n")
    lines = path.read_bytes().splitlines()
    assert lines == [canonical_audit_json_bytes(item) for item in expected]
    assert expected[0]["previous_hash"] == GENESIS_HASH
    assert expected[1]["previous_hash"] == expected[0]["event_hash"]
    assert expected[1]["event_hash"] == final_hash
    assert expected[1]["parent_span_id"] == FIRST_SPAN_ID


def test_chain_properties_and_attributes_are_detached_from_caller(tmp_path):
    path = tmp_path / "audit.jsonl"
    attributes = {"agent": "claude-code", "model": "model-1", "trial": 1}
    with AuditChain(path, "run-1", trace_id=TRACE_ID) as chain:
        event = chain.append("agent.started", attributes, timestamp=TIMESTAMP)
        attributes["trial"] = 999
        assert chain.trace_id == TRACE_ID
        assert chain.event_count == 1
        assert chain.final_hash == event.event_hash
        assert event.attributes["trial"] == 1
    with pytest.raises(AuditError, match="closed"):
        chain.append("late")


def test_chain_generates_w3c_sized_lowercase_trace_and_span_ids(tmp_path):
    path = tmp_path / "audit.jsonl"
    with AuditChain(path, "run-1") as chain:
        event = chain.append(
            "agent.started",
            {"agent": "claude-code", "model": "model-1", "trial": 1},
        )
    assert len(chain.trace_id) == 32
    assert chain.trace_id == chain.trace_id.lower()
    int(chain.trace_id, 16)
    assert len(event.span_id) == 16
    int(event.span_id, 16)


def test_chain_refuses_existing_and_symlink_paths(tmp_path):
    existing = tmp_path / "existing.jsonl"
    existing.write_text("preserve me", encoding="utf-8")
    with pytest.raises(AuditError, match="already exists"):
        AuditChain(existing, "run-1")
    assert existing.read_text(encoding="utf-8") == "preserve me"

    target = tmp_path / "target.jsonl"
    target.write_text("preserve target", encoding="utf-8")
    symlink = tmp_path / "audit.jsonl"
    symlink.symlink_to(target)
    with pytest.raises(AuditError, match="already exists"):
        AuditChain(symlink, "run-1")
    assert target.read_text(encoding="utf-8") == "preserve target"


def test_chain_rejects_oversize_event_without_advancing_state(tmp_path):
    path = tmp_path / "audit.jsonl"
    with AuditChain(path, "r" * MAX_AUDIT_EVENT_BYTES, trace_id=TRACE_ID) as chain:
        with pytest.raises(AuditError, match="exceeds"):
            chain.append(
                "agent.started",
                {"agent": "claude-code", "model": "model-1", "trial": 1},
            )
        assert chain.event_count == 0
        assert chain.final_hash is None
        assert path.read_bytes() == b""


def test_verify_valid_chain_with_expected_bindings(tmp_path):
    path = tmp_path / "audit.jsonl"
    _, final_hash = _make_chain(path)

    result = verify_audit_chain(
        path,
        expected_final_hash=final_hash,
        expected_run_id="run-1",
    )

    assert result.ok
    assert result.failures == []
    assert result.event_count == 2
    assert result.final_hash == final_hash
    assert result.run_id == "run-1"
    assert result.trace_id == TRACE_ID


def test_verify_detects_payload_tamper_even_when_json_remains_canonical(tmp_path):
    path = tmp_path / "audit.jsonl"
    _make_chain(path)
    events = _read_events(path)
    events[0]["attributes"]["trial"] = 2
    _write_events(path, events)

    result = verify_audit_chain(path)

    assert not result.ok
    assert _codes(result) == {"event_hash_mismatch"}
    assert result.failures[0].line == 1


def test_verify_detects_broken_previous_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    _make_chain(path)
    events = _read_events(path)
    events[1]["previous_hash"] = "e" * 64
    _rehash(events[1])
    _write_events(path, events)

    result = verify_audit_chain(path)

    assert _codes(result) == {"previous_hash_mismatch"}
    assert result.event_count == 1


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("sequence", 7, "sequence_mismatch"),
        ("trace_id", "e" * 32, "trace_id_mismatch"),
        ("run_id", "another-run", "run_id_mismatch"),
    ],
)
def test_verify_detects_chain_identity_and_sequence_mismatches(
    tmp_path, field, value, code
):
    path = tmp_path / "audit.jsonl"
    _make_chain(path)
    events = _read_events(path)
    events[1][field] = value
    _rehash(events[1])
    _write_events(path, events)

    result = verify_audit_chain(path)

    assert _codes(result) == {code}


def test_verify_detects_expected_run_and_final_hash_mismatches(tmp_path):
    path = tmp_path / "audit.jsonl"
    _make_chain(path)

    run_result = verify_audit_chain(path, expected_run_id="other-run")
    hash_result = verify_audit_chain(path, expected_final_hash="f" * 64)

    assert _codes(run_result) == {"run_id_mismatch"}
    assert _codes(hash_result) == {"final_hash_mismatch"}
    assert hash_result.event_count == 2


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda event: event.update(sequence="0"), "sequence_invalid"),
        (
            lambda event: event.update(timestamp="2026-07-14 12:00:00"),
            "timestamp_invalid",
        ),
        (lambda event: event.update(trace_id="A" * 32), "trace_id_invalid"),
        (lambda event: event.update(run_id=""), "run_id_invalid"),
        (lambda event: event.update(previous_hash="0" * 63), "previous_hash_invalid"),
        (lambda event: event.update(event_hash="0" * 63), "event_hash_invalid"),
        (lambda event: event.update(extra=True), "schema_invalid"),
    ],
)
def test_verify_classifies_strict_schema_failures(tmp_path, mutate, code):
    path = tmp_path / "audit.jsonl"
    event = _event_data()
    mutate(event)
    path.write_bytes(canonical_audit_json_bytes(event) + b"\n")

    result = verify_audit_chain(path)

    assert not result.ok
    assert _codes(result) == {code}


def test_verify_rejects_sensitive_attributes_without_echoing_values(tmp_path):
    path = tmp_path / "audit.jsonl"
    event = _event_data(attributes={"api_key": "do-not-echo-this-value"})
    path.write_bytes(canonical_audit_json_bytes(event) + b"\n")

    result = verify_audit_chain(path)

    assert _codes(result) == {"schema_invalid"}
    assert "do-not-echo-this-value" not in result.failures[0].message


@pytest.mark.parametrize(
    ("event_type", "attributes"),
    [
        ("unsupported.event", {}),
        ("agent.started", {"system_prompt": "do-not-echo-system-prompt"}),
        ("agent.completed", {"response_body": "do-not-echo-response-body"}),
        (
            "agent.started",
            {
                "agent": "claude-code",
                "model": {"system_prompt": "do-not-echo-nested-prompt"},
                "trial": 1,
            },
        ),
    ],
)
def test_verify_fails_closed_for_unknown_event_types_and_attributes(
    tmp_path, event_type, attributes
):
    path = tmp_path / "audit.jsonl"
    event = _event_data(event_type=event_type, attributes=attributes)
    _rehash(event)
    path.write_bytes(canonical_audit_json_bytes(event) + b"\n")

    result = verify_audit_chain(path)

    assert _codes(result) == {"schema_invalid"}
    failure_message = result.failures[0].message
    assert "do-not-echo-system-prompt" not in failure_message
    assert "do-not-echo-response-body" not in failure_message
    assert "do-not-echo-nested-prompt" not in failure_message


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"", "empty_chain"),
        (b"not-json\n", "invalid_json"),
        (b'{"x":"\xff"}\n', "invalid_utf8"),
        (b'{"a":1,"a":2}\n', "duplicate_json_key"),
        (b'{"a":{"b":1,"b":2}}\n', "duplicate_json_key"),
        (b'{"a": 1}\n', "noncanonical_json"),
        (b'{"a":1}', "noncanonical_json"),
        (b'{"value":NaN}\n', "invalid_json"),
        (b"x" * (MAX_AUDIT_EVENT_BYTES + 1) + b"\n", "event_too_large"),
    ],
)
def test_verify_never_raises_for_malformed_jsonl(tmp_path, raw, code):
    path = tmp_path / "audit.jsonl"
    path.write_bytes(raw)

    result = verify_audit_chain(path)

    assert not result.ok
    assert _codes(result) == {code}


def test_verify_rejects_symlink_nonregular_and_missing_paths(tmp_path):
    valid = tmp_path / "valid.jsonl"
    _make_chain(valid)
    symlink = tmp_path / "link.jsonl"
    symlink.symlink_to(valid)
    directory = tmp_path / "directory"
    directory.mkdir()

    assert _codes(verify_audit_chain(symlink)) == {"symlink_rejected"}
    assert _codes(verify_audit_chain(directory)) == {"not_regular_file"}
    assert _codes(verify_audit_chain(tmp_path / "missing")) == {"file_unreadable"}


def test_verify_invalid_expectations_and_path_types_return_failures(tmp_path):
    path = tmp_path / "audit.jsonl"
    _make_chain(path)

    assert _codes(verify_audit_chain(path, expected_final_hash="bad")) == {
        "expected_final_hash_invalid"
    }
    assert _codes(verify_audit_chain(path, expected_run_id="")) == {
        "expected_run_id_invalid"
    }
    assert _codes(verify_audit_chain(None)) == {"file_unreadable"}
