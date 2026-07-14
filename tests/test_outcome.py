from types import SimpleNamespace

from agent_eval.evaluators.tests import TestResults as EvalTestResults
from agent_eval.metrics import AgentMetrics, JudgeResult, ScanResults
from agent_eval.outcome import AcceptancePolicy, evaluate_outcome


def _record(**overrides):
    values = {
        "correctness": EvalTestResults(
            total=2,
            passed=2,
            command_exit_code=0,
            coverage_percent=92,
        ),
        "efficiency": AgentMetrics(
            wall_time_s=10,
            tokens_in=100,
            tokens_out=20,
            cost_usd=0.04,
        ),
        "scans": ScanResults(
            lint_errors=0,
            sec_findings_high=0,
            secrets_found=0,
            vulns=0,
            scanner_status={"ruff": "ok", "gitleaks": "ok"},
        ),
        "judge": JudgeResult(weighted_score=4.2),
        "assurance": SimpleNamespace(passed=True),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_acceptance_policy_accepts_complete_evidence():
    policy = AcceptancePolicy(
        min_coverage_percent=90,
        min_judge_score=4,
        required_scanners=["ruff", "gitleaks"],
        max_lint_errors=0,
        max_security_findings_high=0,
        max_secrets=0,
        max_vulnerabilities=0,
        max_wall_time_s=20,
        max_total_tokens=150,
        max_cost_usd=0.05,
        require_challenges_passed=True,
    )

    outcome = evaluate_outcome(_record(), policy)

    assert outcome.status == "accepted"
    assert outcome.reasons == []
    assert all(check.passed for check in outcome.checks)


def test_configured_missing_evidence_fails_closed():
    record = _record(
        scans=ScanResults(scanner_status={"ruff": "unavailable"}),
        judge=JudgeResult(),
    )
    policy = AcceptancePolicy(
        min_judge_score=3,
        required_scanners=["ruff", "gitleaks"],
        max_secrets=0,
    )

    outcome = evaluate_outcome(record, policy)

    assert outcome.status == "rejected"
    assert any("scanner ruff" in reason for reason in outcome.reasons)
    assert any("scanner gitleaks" in reason for reason in outcome.reasons)
    assert any("judge score" in reason for reason in outcome.reasons)
    assert any("secrets" in reason for reason in outcome.reasons)


def test_infrastructure_failure_is_distinct_from_rejection():
    correctness = EvalTestResults(infra_error="eval pod OOMKilled")

    outcome = evaluate_outcome(
        _record(correctness=correctness), AcceptancePolicy()
    )

    assert outcome.status == "infra_error"
    assert outcome.reasons == ["eval pod OOMKilled"]
