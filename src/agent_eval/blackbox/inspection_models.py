"""Optional internal evidence, independent of the output-evaluation suite."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


def relative_path(value: str) -> str:
    if (
        not value
        or PurePosixPath(value).is_absolute()
        or ".." in value.split("/")
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(
            "source paths must be relative and stay inside the selected root"
        )
    return value


class SourceSelection(InspectionModel):
    include: list[str] = Field(min_length=1, max_length=30)
    exclude: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def safe_patterns(self):
        for pattern in self.include + self.exclude:
            relative_path(pattern)
        return self


CheckKind = Literal[
    "source_contains",
    "source_not_contains",
    "source_geval",
    "tools_required",
    "tools_forbidden",
    "tool_sequence",
    "tool_budget",
    "trace_no_errors",
    "trace_geval",
]


class InspectionCheck(InspectionModel):
    name: str = Field(min_length=1, max_length=100)
    kind: CheckKind
    path: str | None = None
    text: str | None = Field(default=None, min_length=1, max_length=10_000)
    tools: list[str] | None = Field(default=None, min_length=1, max_length=100)
    max_calls: int | None = Field(default=None, ge=0, le=10_000)
    criteria: str | None = Field(default=None, min_length=1, max_length=10_000)
    threshold: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def required_fields(self):
        required = {
            "source_contains": {"path", "text"},
            "source_not_contains": {"path", "text"},
            "source_geval": {"criteria"},
            "tools_required": {"tools"},
            "tools_forbidden": {"tools"},
            "tool_sequence": {"tools"},
            "tool_budget": {"max_calls"},
            "trace_no_errors": set(),
            "trace_geval": {"criteria"},
        }[self.kind]
        fields = {"path", "text", "tools", "max_calls", "criteria"}
        if {name for name in fields if getattr(self, name) is not None} != required:
            raise ValueError("check parameters do not match its kind")
        if self.path is not None:
            relative_path(self.path)
            if any(char in self.path for char in "*?["):
                raise ValueError("a source text check names one exact file")
        if self.tools and any(
            not tool.strip() or len(tool) > 200 for tool in self.tools
        ):
            raise ValueError("tool names must contain 1 to 200 characters")
        if not self.kind.endswith("geval") and self.threshold != 1:
            raise ValueError("deterministic inspection checks require threshold 1")
        return self


class InspectionProfile(InspectionModel):
    schema_version: Literal["1.0"]
    source: SourceSelection | None = None
    checks: list[InspectionCheck] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def configured_source(self):
        names = [check.name for check in self.checks]
        if len(names) != len(set(names)):
            raise ValueError("inspection check names must be unique")
        needs_source = any(check.kind.startswith("source_") for check in self.checks)
        if needs_source != (self.source is not None):
            raise ValueError("source selection is required only for source checks")
        return self


class SourceFile(InspectionModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str

    @model_validator(mode="after")
    def bound_content(self):
        relative_path(self.path)
        if hashlib.sha256(self.content.encode("utf-8")).hexdigest() != self.sha256:
            raise ValueError("source content does not match its digest")
        return self


class SourceSnapshot(InspectionModel):
    files: list[SourceFile] = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bound_tree(self):
        names = [file.path for file in self.files]
        if names != sorted(set(names)):
            raise ValueError("source paths must be sorted and unique")
        data = [{"path": file.path, "sha256": file.sha256} for file in self.files]
        raw = json.dumps(
            data, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise ValueError("source manifest does not match its digest")
        return self


class TraceSpan(InspectionModel):
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str = Field(min_length=1, max_length=500)
    kind: Literal["agent", "tool", "model", "internal"] = "internal"
    status: Literal["ok", "error", "unset"] = "unset"
    start_ns: int = Field(ge=0)
    end_ns: int = Field(ge=0)
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    arguments: JsonValue = None
    output: JsonValue = None

    @model_validator(mode="after")
    def valid_span(self):
        if int(self.span_id, 16) == 0 or (
            self.parent_span_id and int(self.parent_span_id, 16) == 0
        ):
            raise ValueError("span IDs must be nonzero")
        if self.end_ns < self.start_ns:
            raise ValueError("span end precedes its start")
        if (self.kind == "tool") != (self.tool_name is not None):
            raise ValueError("tool spans require tool_name; other spans must omit it")
        return self


class TraceRecord(InspectionModel):
    case_id: str = Field(min_length=1, max_length=200)
    trial: int = Field(ge=1, le=10_000)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    complete: bool
    spans: list[TraceSpan] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def rooted_trace(self):
        if not int(self.trace_id, 16):
            raise ValueError("trace ID must be nonzero")
        by_id = {span.span_id: span for span in self.spans}
        if len(by_id) != len(self.spans) or self.root_span_id not in by_id:
            raise ValueError("trace requires unique span IDs and its declared root")
        root = by_id[self.root_span_id]
        if root.parent_span_id is not None:
            raise ValueError("the exported root must have no parent")
        for span in self.spans:
            visited = set()
            current = span.span_id
            while current != self.root_span_id:
                if current in visited or current not in by_id:
                    raise ValueError("trace is cyclic or has a missing parent")
                visited.add(current)
                current = by_id[current].parent_span_id
        return self


InspectionStatus = Literal["accepted", "rejected", "unavailable", "not_requested"]
EvidenceError = Literal[
    "source_missing",
    "source_unreadable",
    "source_changed",
    "source_path_missing",
    "trace_missing",
    "trace_invalid",
    "trace_incomplete",
    "trace_binding_mismatch",
    "trace_identity_mismatch",
    "observation_unavailable",
    "judge_error",
]


class InspectionResult(InspectionModel):
    name: str
    kind: CheckKind
    scope: Literal["source", "trace"]
    case_id: str | None = None
    trial: int | None = None
    status: Literal["accepted", "rejected", "unavailable"]
    score: float | None = None
    reason: str = ""
    error: EvidenceError | None = None


class InspectionSummary(InspectionModel):
    status: InspectionStatus
    accepted: int
    rejected: int
    unavailable: int
    score: float | None


class InspectionReport(InspectionModel):
    schema_version: Literal["1.0"] = "1.0"
    required: bool
    status: InspectionStatus
    profile_sha256: str
    source_sha256: str | None
    traces_sha256: str | None
    source: InspectionSummary
    trace: InspectionSummary
    results: list[InspectionResult]
