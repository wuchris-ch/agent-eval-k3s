"""Validation for versioned, executable pull-request review corpora."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .review_benchmark import BenchmarkManifest, load_manifest

CORPUS_SCHEMA_VERSION = "1.0"
REPRODUCER_TIMEOUT_SECONDS = 60
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def _safe_relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("must be a safe relative path")
    return value


def _resolve(root: Path, value: str) -> Path:
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes corpus root: {value}") from exc
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Reproducer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: list[str]
    base_cwd: str
    head_cwd: str
    expected_base_exit: int = 0
    expected_head_exit: int

    @field_validator("command")
    @classmethod
    def _command_is_argv(cls, value: list[str]) -> list[str]:
        if not value or any(not item or "\x00" in item for item in value):
            raise ValueError("reproducer command must be a non-empty argv list")
        return value

    @field_validator("base_cwd", "head_cwd")
    @classmethod
    def _safe_paths(cls, value: str) -> str:
        return _safe_relative(value)


class CorpusCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["faulty", "clean"]
    diff: str
    reproducer: Reproducer
    artifact_sha256: dict[str, str]

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not _CASE_ID.fullmatch(value):
            raise ValueError("case id must be a safe path segment")
        return value

    @field_validator("diff")
    @classmethod
    def _safe_diff(cls, value: str) -> str:
        return _safe_relative(value)

    @field_validator("artifact_sha256")
    @classmethod
    def _valid_artifact_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("artifact_sha256 must bind at least one file")
        for path, digest in value.items():
            _safe_relative(path)
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"invalid SHA-256 digest for {path}")
        return value

    @model_validator(mode="after")
    def _faulty_case_has_distinguishing_exits(self) -> "CorpusCase":
        if (
            self.kind == "faulty"
            and self.reproducer.expected_base_exit
            == self.reproducer.expected_head_exit
        ):
            raise ValueError(
                "faulty case must declare different expected base and head exits"
            )
        return self


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    corpus_id: str
    version: str
    benchmark_manifest: str
    benchmark_sha256: str
    cases: list[CorpusCase]

    @field_validator("benchmark_manifest")
    @classmethod
    def _safe_benchmark(cls, value: str) -> str:
        return _safe_relative(value)

    @field_validator("benchmark_sha256")
    @classmethod
    def _valid_benchmark_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("benchmark_sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def _unique_cases(self) -> "CorpusManifest":
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("corpus contains duplicate case ids")
        return self


class ReproducerResult(BaseModel):
    case_id: str
    base_exit: int | None
    head_exit: int | None
    passed: bool
    detail: str = ""


class CorpusValidation(BaseModel):
    corpus_id: str
    version: str
    valid: bool
    errors: list[str] = Field(default_factory=list)
    reproducers: list[ReproducerResult] = Field(default_factory=list)


def load_corpus(path: Path | str) -> tuple[CorpusManifest, Path]:
    manifest_path = Path(path).resolve()
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return CorpusManifest.model_validate(raw), manifest_path.parent


def _added_lines(diff: str) -> set[tuple[str, int]]:
    locations: set[tuple[str, int]] = set()
    current_path: str | None = None
    head_line: int | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            head_line = None
        elif line.startswith("+++ "):
            raw = line[4:].strip()
            current_path = raw[2:] if raw.startswith("b/") else raw
            if raw == "/dev/null":
                current_path = None
        elif match := _HUNK.match(line):
            head_line = int(match.group("start")) if current_path else None
        elif current_path is not None and head_line is not None:
            if line.startswith("+") and not line.startswith("+++"):
                locations.add((current_path, head_line))
                head_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            elif not line.startswith("\\ No newline"):
                head_line += 1
    return locations


def _run_reproducer(root: Path, case: CorpusCase) -> ReproducerResult:
    exits: list[int | None] = []
    details = []
    for label, cwd_value in (
        ("base", case.reproducer.base_cwd),
        ("head", case.reproducer.head_cwd),
    ):
        cwd = _resolve(root, cwd_value)
        try:
            proc = subprocess.run(
                case.reproducer.command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=REPRODUCER_TIMEOUT_SECONDS,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            exits.append(proc.returncode)
            if proc.returncode:
                details.append(f"{label}: {(proc.stdout + proc.stderr)[-500:]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            exits.append(None)
            details.append(f"{label}: {type(exc).__name__}")
    base_exit, head_exit = exits
    passed = (
        base_exit == case.reproducer.expected_base_exit
        and head_exit == case.reproducer.expected_head_exit
    )
    return ReproducerResult(
        case_id=case.id,
        base_exit=base_exit,
        head_exit=head_exit,
        passed=passed,
        detail="; ".join(details),
    )


def _case_artifacts(root: Path, case: CorpusCase) -> tuple[set[str], list[str]]:
    """Inventory regular files without following symlinks."""

    case_root = root / "cases" / case.id
    if case_root.is_symlink():
        return set(), [f"{case.id}: symlink is not allowed: cases/{case.id}"]
    if not case_root.is_dir():
        return set(), [f"{case.id}: case artifact subtree is missing"]

    regular_files: set[str] = set()
    errors: list[str] = []
    for artifact in case_root.rglob("*"):
        relative = artifact.relative_to(root).as_posix()
        if artifact.is_symlink():
            errors.append(f"{case.id}: symlink is not allowed: {relative}")
        elif artifact.is_file():
            regular_files.add(relative)
    return regular_files, errors


def _validate_case_artifacts(root: Path, case: CorpusCase) -> list[str]:
    actual, errors = _case_artifacts(root, case)
    prefix = ("cases", case.id)
    declared = set(case.artifact_sha256)
    declared_in_case = {
        relative
        for relative in declared
        if PurePosixPath(relative).parts[:2] == prefix
    }

    for relative in sorted(declared - declared_in_case):
        errors.append(f"{case.id}: artifact is outside its case subtree: {relative}")
    for relative in sorted(actual - declared_in_case):
        errors.append(f"{case.id}: unlisted artifact: {relative}")
    for relative in sorted(declared_in_case - actual):
        errors.append(f"{case.id}: artifact missing or not a regular file: {relative}")

    for relative in sorted(actual & declared_in_case):
        artifact = root / relative
        if _sha256(artifact) != case.artifact_sha256[relative]:
            errors.append(f"{case.id}: artifact hash mismatch: {relative}")
    return errors


def validate_corpus(path: Path | str, *, execute: bool = True) -> CorpusValidation:
    """Validate hashes, gold labels, and executable base/head behavior."""

    manifest, root = load_corpus(path)
    benchmark_path = _resolve(root, manifest.benchmark_manifest)
    errors: list[str] = []
    if _sha256(benchmark_path) != manifest.benchmark_sha256:
        errors.append("benchmark manifest hash mismatch")
    benchmark: BenchmarkManifest = load_manifest(benchmark_path)
    benchmark_by_id = {case.id: case for case in benchmark.cases}
    corpus_by_id = {case.id: case for case in manifest.cases}
    if set(benchmark_by_id) != set(corpus_by_id):
        errors.append("corpus and benchmark case ids differ")

    reproducers = []
    for case in manifest.cases:
        benchmark_case = benchmark_by_id.get(case.id)
        if benchmark_case is None:
            continue
        if case.kind == "clean" and benchmark_case.expected_findings:
            errors.append(f"{case.id}: clean case has expected findings")
        if case.kind == "faulty" and not benchmark_case.expected_findings:
            errors.append(f"{case.id}: faulty case has no expected findings")

        diff_path = _resolve(root, case.diff)
        if not diff_path.is_file():
            errors.append(f"{case.id}: diff artifact is missing")
            added = set()
        else:
            added = _added_lines(diff_path.read_text(encoding="utf-8"))
        for finding in benchmark_case.expected_findings:
            if not any(
                (finding.file, line) in added
                for line in range(finding.line_start, finding.line_end + 1)
            ):
                errors.append(
                    f"{case.id}: finding {finding.id} is not on an added diff line"
                )

        errors.extend(_validate_case_artifacts(root, case))

        if execute:
            result = _run_reproducer(root, case)
            reproducers.append(result)
            if not result.passed:
                errors.append(f"{case.id}: reproducer did not distinguish base/head")

    return CorpusValidation(
        corpus_id=manifest.corpus_id,
        version=manifest.version,
        valid=not errors,
        errors=errors,
        reproducers=reproducers,
    )
