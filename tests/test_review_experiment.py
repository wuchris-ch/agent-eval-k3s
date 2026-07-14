from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_eval.review_experiment import load_experiment, run_experiment


def _write_manifest(root: Path, *, clean_case: bool = True) -> None:
    cases = [
        {
            "id": "bug",
            "changed_lines": 10,
            "expected": [
                {
                    "id": "BUG-1",
                    "severity": "major",
                    "category": "correctness",
                    "file": "src/app.py",
                    "line_start": 10,
                }
            ],
        }
    ]
    if clean_case:
        cases.append({"id": "clean", "changed_lines": 10, "expected": []})
    (root / "benchmark.yaml").write_text(
        yaml.safe_dump({"cases": cases}), encoding="utf-8"
    )


def _write_output(
    directory: Path,
    case_id: str,
    findings: list[dict],
    *,
    latency_s: float | None = None,
    tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    metrics = {
        key: value
        for key, value in {
            "latency_s": latency_s,
            "tokens": tokens,
            "cost_usd": cost_usd,
        }.items()
        if value is not None
    }
    (directory / f"{case_id}.json").write_text(
        json.dumps({"findings": findings, "metrics": metrics}), encoding="utf-8"
    )


def _finding(
    *,
    severity: str = "major",
    file: str = "src/app.py",
    category: str = "correctness",
    line: int = 10,
) -> dict:
    return {
        "severity": severity,
        "category": category,
        "file": file,
        "line": line,
    }


def _write_experiment(root: Path, payload: dict) -> Path:
    path = root / "experiment.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _system(result, system_id: str):
    return next(system for system in result.systems if system.system_id == system_id)


def test_repeated_single_reviewers_have_statistics_pairs_budgets_and_frontier(
    tmp_path,
):
    _write_manifest(tmp_path)
    systems = [
        {
            "id": "baseline",
            "mode": "single",
            "trials": [
                {"id": "t1", "outputs": "outputs/baseline/t1"},
                {"id": "t2", "outputs": "outputs/baseline/t2"},
            ],
        },
        {
            "id": "improved",
            "mode": "single",
            "trials": [
                {"id": "t1", "outputs": "outputs/improved/t1"},
                {"id": "t2", "outputs": "outputs/improved/t2"},
            ],
        },
    ]
    experiment = _write_experiment(
        tmp_path,
        {
            "version": 1,
            "benchmark": "benchmark.yaml",
            "baseline": "baseline",
            "budgets": {
                "max_fp_per_case": 0,
                "max_latency_s": 3,
                "max_tokens": 100,
                "max_cost_usd": 0.1,
            },
            "systems": systems,
        },
    )

    for case_id in ("bug", "clean"):
        _write_output(
            tmp_path / "outputs/baseline/t1",
            case_id,
            [],
            latency_s=4,
            tokens=100,
            cost_usd=0.1,
        )
        _write_output(
            tmp_path / "outputs/baseline/t2",
            case_id,
            [_finding()] if case_id == "bug" else [],
            latency_s=6,
            tokens=200,
            cost_usd=0.2,
        )
        for trial in ("t1", "t2"):
            _write_output(
                tmp_path / f"outputs/improved/{trial}",
                case_id,
                [_finding()] if case_id == "bug" else [],
                latency_s=2,
                tokens=80,
                cost_usd=0.05,
            )

    result = run_experiment(experiment)
    baseline = _system(result, "baseline")
    improved = _system(result, "improved")

    assert len(baseline.trials) == 2
    assert all(
        case.status == "scored"
        for trial in baseline.trials
        for case in trial.benchmark.cases
    )
    assert baseline.statistics.f1.mean == pytest.approx(0.5)
    assert baseline.statistics.f1.sample_stdev == pytest.approx(2**-0.5)
    assert baseline.statistics.precision.count == 1
    assert baseline.statistics.precision.completeness == 0.5
    assert baseline.statistics.latency_s.mean == 5
    assert baseline.statistics.latency_s.sample_stdev == pytest.approx(1.154700538)
    assert baseline.completeness.source_output_rate == 1
    assert baseline.finding_stability.mean_jaccard == 0.5
    assert baseline.finding_stability.sample_stdev == pytest.approx(2**-0.5)
    assert baseline.budget.eligible is False

    assert improved.statistics.f1.mean == 1
    assert improved.statistics.f1.sample_stdev == 0
    assert improved.finding_stability.mean_jaccard == 1
    assert improved.budget.eligible is True

    comparison = result.paired_comparisons[0]
    assert comparison.baseline_system_id == "baseline"
    assert comparison.candidate_system_id == "improved"
    assert (comparison.wins, comparison.ties, comparison.losses) == (1, 3, 0)
    assert comparison.compared_pairs == comparison.expected_pairs == 4
    assert comparison.f1_delta.mean == 0.25
    assert comparison.f1_delta.sample_stdev == 0.5
    assert comparison.latency_delta_s.mean == -3
    assert comparison.token_delta.mean == -70
    assert comparison.cost_delta_usd.mean == pytest.approx(-0.1)

    assert [point.system_id for point in result.efficiency_frontier] == ["improved"]
    assert result.efficiency_frontier[0].budget_eligible is True
    json.dumps(result.model_dump(mode="json"))


def test_panel_votes_once_per_member_and_combines_parallel_costs(tmp_path):
    _write_manifest(tmp_path, clean_case=False)
    experiment = _write_experiment(
        tmp_path,
        {
            "version": 1,
            "benchmark": "benchmark.yaml",
            "baseline": "solo",
            "budgets": {
                "max_fp_per_case": 0,
                "max_latency_s": 5,
                "max_tokens": 60,
                "max_cost_usd": 0.6,
            },
            "systems": [
                {
                    "id": "solo",
                    "mode": "single",
                    "trials": [{"id": "t1", "outputs": "outputs/solo/t1"}],
                },
                {
                    "id": "panel",
                    "mode": "panel",
                    "members": ["alpha", "beta", "gamma"],
                    "quorum": 2,
                    "trials": [{"id": "t1", "outputs": "outputs/panel/t1"}],
                },
            ],
        },
    )
    _write_output(
        tmp_path / "outputs/solo/t1",
        "bug",
        [_finding()],
        latency_s=10,
        tokens=100,
        cost_usd=1,
    )
    _write_output(
        tmp_path / "outputs/panel/t1/alpha",
        "bug",
        [
            _finding(severity="minor", file=r".\src\app.py"),
            _finding(severity="minor", file="src/./app.py"),
            _finding(severity="blocker", line=30),
        ],
        latency_s=2,
        tokens=10,
        cost_usd=0.1,
    )
    _write_output(
        tmp_path / "outputs/panel/t1/beta",
        "bug",
        [_finding(severity="Major")],
        latency_s=5,
        tokens=20,
        cost_usd=0.2,
    )
    _write_output(
        tmp_path / "outputs/panel/t1/gamma",
        "bug",
        [],
        latency_s=3,
        tokens=30,
        cost_usd=0.3,
    )

    result = run_experiment(load_experiment(experiment))
    assert result.model_dump() == run_experiment(experiment).model_dump()
    panel = _system(result, "panel")
    case = panel.trials[0].cases[0]

    assert case.complete is True
    assert case.expected_source_outputs == case.complete_source_outputs == 3
    assert len(case.findings) == 1
    assert case.findings[0].file == "src/app.py"
    assert case.findings[0].severity == "major"
    assert case.metrics.latency_s == 5
    assert case.metrics.tokens == 60
    assert case.metrics.cost_usd == pytest.approx(0.6)
    assert panel.trials[0].benchmark.metrics.tp == 1
    assert panel.trials[0].benchmark.metrics.fp == 0
    assert panel.statistics.latency_s.mean == 5
    assert panel.statistics.tokens.mean == 60
    assert panel.statistics.cost_usd.mean == pytest.approx(0.6)
    assert panel.budget.eligible is True
    assert panel.finding_stability.expected_pair_count == 0
    assert panel.finding_stability.mean_jaccard is None


def test_efficiency_frontier_excludes_budget_ineligible_system(tmp_path):
    _write_manifest(tmp_path, clean_case=False)
    experiment = _write_experiment(
        tmp_path,
        {
            "version": 1,
            "benchmark": "benchmark.yaml",
            "baseline": "solo",
            "budgets": {"max_latency_s": 0.5},
            "systems": [
                {
                    "id": "solo",
                    "mode": "single",
                    "trials": [{"id": "t1", "outputs": "outputs/solo"}],
                }
            ],
        },
    )
    _write_output(
        tmp_path / "outputs/solo",
        "bug",
        [_finding()],
        latency_s=1,
        tokens=10,
        cost_usd=0.01,
    )

    result = run_experiment(experiment)

    assert _system(result, "solo").budget.eligible is False
    assert result.efficiency_frontier == []


def test_missing_malformed_outputs_and_metrics_are_visible_and_fail_closed(tmp_path):
    _write_manifest(tmp_path)
    experiment = _write_experiment(
        tmp_path,
        {
            "version": 1,
            "benchmark": "benchmark.yaml",
            "baseline": "complete",
            "budgets": {"max_latency_s": 10, "max_tokens": 1000},
            "systems": [
                {
                    "id": "complete",
                    "mode": "single",
                    "trials": [{"id": "t1", "outputs": "outputs/complete"}],
                },
                {
                    "id": "partial",
                    "mode": "single",
                    "trials": [{"id": "t1", "outputs": "outputs/partial"}],
                },
            ],
        },
    )
    for case_id in ("bug", "clean"):
        _write_output(
            tmp_path / "outputs/complete",
            case_id,
            [_finding()] if case_id == "bug" else [],
            latency_s=1,
            tokens=20,
            cost_usd=0.01,
        )
    _write_output(tmp_path / "outputs/partial", "bug", [_finding()])
    (tmp_path / "outputs/partial/clean.json").write_text(
        "{not valid JSON", encoding="utf-8"
    )

    result = run_experiment(experiment)
    partial = _system(result, "partial")

    assert partial.completeness.complete_case_trials == 1
    assert partial.completeness.expected_case_trials == 2
    assert partial.completeness.source_output_rate == 0.5
    assert partial.completeness.latency_rate == 0
    assert partial.statistics.latency_s.mean is None
    assert partial.budget.eligible is False
    assert "review outputs are incomplete" in partial.budget.failures
    assert "latency_s is incomplete" in partial.budget.failures
    assert "tokens is incomplete" in partial.budget.failures
    assert partial.trials[0].benchmark.cases[1].status == "incomplete_prediction"
    assert partial.trials[0].cases[1].issues

    comparison = result.paired_comparisons[0]
    assert comparison.expected_pairs == 2
    assert comparison.compared_pairs == 1
    assert comparison.unavailable_pairs == 1
    assert comparison.pairs[1].outcome == "unavailable"
    assert [point.system_id for point in result.efficiency_frontier] == ["complete"]
    assert str(tmp_path) not in json.dumps(result.model_dump(mode="json"))


def test_experiment_schema_is_strict_and_paths_cannot_escape(tmp_path):
    _write_manifest(tmp_path, clean_case=False)
    base = {
        "version": 1,
        "benchmark": "benchmark.yaml",
        "baseline": "solo",
        "systems": [
            {
                "id": "solo",
                "mode": "single",
                "trials": [{"id": "t1", "outputs": "outputs/solo"}],
            }
        ],
    }

    valid_path = _write_experiment(tmp_path, base)
    assert load_experiment(valid_path).systems[0].trials[0].outputs == "outputs/solo"

    invalid = dict(base, surprise=True)
    with pytest.raises(ValidationError, match="surprise"):
        load_experiment(_write_experiment(tmp_path, invalid))

    traversal = dict(base, benchmark="../benchmark.yaml")
    with pytest.raises(ValidationError, match="safe relative path"):
        load_experiment(_write_experiment(tmp_path, traversal))

    for invalid_version in (2, True, "1"):
        bad_version = dict(base, version=invalid_version)
        with pytest.raises(ValidationError, match="version"):
            load_experiment(_write_experiment(tmp_path, bad_version))

    panel = dict(base)
    panel["systems"] = [
        {
            "id": "solo",
            "mode": "panel",
            "members": ["a", "b"],
            "quorum": 3,
            "trials": [{"id": "t1", "outputs": "outputs/panel"}],
        }
    ]
    with pytest.raises(ValidationError, match="quorum"):
        load_experiment(_write_experiment(tmp_path, panel))

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    symlink_escape = dict(base)
    symlink_escape["systems"] = [
        {
            "id": "solo",
            "mode": "single",
            "trials": [{"id": "t1", "outputs": "escape/outputs"}],
        }
    ]
    with pytest.raises(ValueError, match="outside the experiment root"):
        load_experiment(_write_experiment(tmp_path, symlink_escape))

    output_dir = tmp_path / "outputs/solo"
    output_dir.mkdir(parents=True)
    outside_output = outside / "bug.json"
    outside_output.write_text(json.dumps({"findings": []}), encoding="utf-8")
    (output_dir / "bug.json").symlink_to(outside_output)
    safe_spec = load_experiment(_write_experiment(tmp_path, base))
    with pytest.raises(ValueError, match="outside the experiment root"):
        run_experiment(safe_spec)


def test_experiment_rejects_duplicate_yaml_keys_and_unpaired_trial_sets(tmp_path):
    _write_manifest(tmp_path, clean_case=False)
    experiment = tmp_path / "experiment.yaml"
    experiment.write_text(
        """
version: 1
version: 1
benchmark: benchmark.yaml
baseline: a
systems: []
""",
        encoding="utf-8",
    )
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        load_experiment(experiment)

    experiment.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "benchmark": "benchmark.yaml",
                "baseline": "a",
                "systems": [
                    {
                        "id": "a",
                        "mode": "single",
                        "trials": [{"id": "t1", "outputs": "outputs/a"}],
                    },
                    {
                        "id": "b",
                        "mode": "single",
                        "trials": [{"id": "t2", "outputs": "outputs/b"}],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="same trial ids"):
        load_experiment(experiment)
