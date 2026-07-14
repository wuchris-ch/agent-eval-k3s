"""Explicit, fail-closed acceptance outcomes for coding-agent trials."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AcceptancePolicy(BaseModel):
    """Task-level evidence required before a trial can be accepted.

    Correctness is required by default.  Optional gates are fail-closed: once
    a threshold or scanner is configured, absent evidence rejects the trial
    rather than looking like a clean result.
    """

    model_config = ConfigDict(extra="forbid")

    require_resolved: bool = True
    min_coverage_percent: float | None = Field(default=None, ge=0, le=100)
    min_judge_score: float | None = Field(default=None, ge=1, le=5)
    required_scanners: list[str] = Field(default_factory=list)
    max_lint_errors: int | None = Field(default=None, ge=0)
    max_security_findings_high: int | None = Field(default=None, ge=0)
    max_secrets: int | None = Field(default=None, ge=0)
    max_vulnerabilities: int | None = Field(default=None, ge=0)
    max_wall_time_s: float | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, ge=0)
    max_cost_usd: float | None = Field(default=None, ge=0)
    require_challenges_passed: bool = False

    @field_validator("required_scanners")
    @classmethod
    def _unique_scanners(cls, value: list[str]) -> list[str]:
        normalized = []
        for scanner in value:
            scanner = scanner.strip().lower()
            if not scanner:
                raise ValueError("required scanner names must not be empty")
            if scanner not in normalized:
                normalized.append(scanner)
        return normalized

    @field_validator("max_wall_time_s", "max_cost_usd")
    @classmethod
    def _finite_numbers(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("acceptance thresholds must be finite")
        return value


class OutcomeCheck(BaseModel):
    name: str
    passed: bool
    observed: str
    requirement: str


class RunOutcome(BaseModel):
    status: Literal["accepted", "rejected", "infra_error"]
    reasons: list[str] = Field(default_factory=list)
    checks: list[OutcomeCheck] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


def _check(
    checks: list[OutcomeCheck],
    reasons: list[str],
    *,
    name: str,
    passed: bool,
    observed: object,
    requirement: str,
    missing: bool = False,
) -> None:
    observed_text = "unavailable" if missing else str(observed)
    checks.append(
        OutcomeCheck(
            name=name,
            passed=passed,
            observed=observed_text,
            requirement=requirement,
        )
    )
    if not passed:
        reasons.append(f"{name}: observed {observed_text}; requires {requirement}")


def evaluate_outcome(record: Any, policy: AcceptancePolicy) -> RunOutcome:
    """Evaluate a ``RunRecord`` without importing it, avoiding model cycles."""

    correctness = record.correctness
    if correctness.infra_error:
        return RunOutcome(
            status="infra_error",
            reasons=[correctness.infra_error],
            checks=[],
        )
    if record.efficiency.infra_error:
        return RunOutcome(
            status="infra_error",
            reasons=[record.efficiency.infra_error],
            checks=[],
        )

    checks: list[OutcomeCheck] = []
    reasons: list[str] = []
    if record.efficiency.agent_exit_code is not None:
        _check(
            checks,
            reasons,
            name="agent command",
            passed=record.efficiency.agent_exit_code == 0,
            observed=f"exit {record.efficiency.agent_exit_code}",
            requirement="exit 0",
        )
    if correctness.integrity_error:
        _check(
            checks,
            reasons,
            name="evaluation integrity",
            passed=False,
            observed=correctness.integrity_error,
            requirement="no unsafe evaluator-control or workspace evidence",
        )
    if policy.require_resolved:
        _check(
            checks,
            reasons,
            name="hidden tests",
            passed=bool(correctness.resolved),
            observed=(
                f"{correctness.passed}/{correctness.total} passed, "
                f"command exit {correctness.command_exit_code}"
            ),
            requirement="resolved with test command exit 0",
        )

    def minimum(name: str, value: float | None, threshold: float | None) -> None:
        if threshold is None:
            return
        _check(
            checks,
            reasons,
            name=name,
            passed=value is not None and value >= threshold,
            observed=value,
            requirement=f">= {threshold:g}",
            missing=value is None,
        )

    def maximum(name: str, value: float | int | None,
                threshold: float | int | None) -> None:
        if threshold is None:
            return
        _check(
            checks,
            reasons,
            name=name,
            passed=value is not None and value <= threshold,
            observed=value,
            requirement=f"<= {threshold:g}",
            missing=value is None,
        )

    minimum("coverage percent", correctness.coverage_percent,
            policy.min_coverage_percent)
    minimum("judge score", record.judge.weighted_score, policy.min_judge_score)

    statuses = getattr(record.scans, "scanner_status", {})
    for scanner in policy.required_scanners:
        state = statuses.get(scanner)
        _check(
            checks,
            reasons,
            name=f"scanner {scanner}",
            passed=state == "ok",
            observed=state,
            requirement="ok",
            missing=state is None,
        )

    maximum("lint errors", record.scans.lint_errors, policy.max_lint_errors)
    maximum(
        "high security findings",
        record.scans.sec_findings_high,
        policy.max_security_findings_high,
    )
    maximum("secrets", record.scans.secrets_found, policy.max_secrets)
    maximum("vulnerabilities", record.scans.vulns, policy.max_vulnerabilities)
    maximum("wall time seconds", record.efficiency.wall_time_s,
            policy.max_wall_time_s)
    total_tokens = None
    if record.efficiency.tokens_in is not None and record.efficiency.tokens_out is not None:
        total_tokens = record.efficiency.tokens_in + record.efficiency.tokens_out
    maximum("total tokens", total_tokens, policy.max_total_tokens)
    maximum("cost USD", record.efficiency.cost_usd, policy.max_cost_usd)

    if policy.require_challenges_passed:
        assurance = getattr(record, "assurance", None)
        passed = assurance is not None and assurance.passed is True
        _check(
            checks,
            reasons,
            name="adversarial challenges",
            passed=passed,
            observed=(assurance.passed if assurance is not None else None),
            requirement="all passed",
            missing=assurance is None,
        )

    return RunOutcome(
        status="accepted" if not reasons else "rejected",
        reasons=reasons,
        checks=checks,
    )
