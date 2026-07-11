import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_eval.cli import app
from agent_eval.review_benchmark import (
    BenchmarkCase,
    BenchmarkManifest,
    ExpectedFinding,
    load_manifest,
    score_benchmark,
)


def _expected(
    finding_id: str,
    *,
    severity: str = "major",
    category: str = "correctness",
    file: str = "src/app.py",
    line_start: int = 10,
    line_end: int | None = None,
) -> ExpectedFinding:
    data = {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "file": file,
        "line_start": line_start,
    }
    if line_end is not None:
        data["line_end"] = line_end
    return ExpectedFinding.model_validate(data)


def _write_predictions(reviews_dir, case_id: str, findings: list[dict]) -> None:
    reviews_dir.mkdir(exist_ok=True)
    (reviews_dir / f"{case_id}.json").write_text(
        json.dumps({"findings": findings})
    )


def test_load_manifest_defaults_and_validates_schema(tmp_path):
    path = tmp_path / "benchmark.yaml"
    path.write_text(
        """
cases:
  - id: range-case
    description: catches a boundary bug
    expected_findings:
      - id: bounds-1
        severity: blocker
        category: correctness
        file: src/range.py
        line_start: 12
  - id: clean-case
"""
    )

    manifest = load_manifest(path)

    assert manifest.cases[0].description == "catches a boundary bug"
    assert manifest.cases[0].changed_lines is None
    assert manifest.cases[0].expected_findings[0].line_end == 12
    assert manifest.cases[1].expected_findings == []

    with pytest.raises(ValidationError, match="line_end"):
        _expected("bad-range", line_start=8, line_end=7)
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        BenchmarkCase(id="bad-lines", changed_lines=-1)
    with pytest.raises(ValidationError, match="duplicate case ids"):
        BenchmarkManifest(
            cases=[BenchmarkCase(id="same"), BenchmarkCase(id="same")]
        )
    with pytest.raises(ValidationError, match="letters, digits"):
        BenchmarkCase(id="../outside")


@pytest.mark.parametrize(
    ("manifest_text", "findings", "invalid_field"),
    [
        (
            """
cases:
  - id: case
    expected:
      - id: bug
        severity: major
        category: correctness
        file: src/app.py
        line_start: true
""",
            [],
            "line_start",
        ),
        (
            """
cases:
  - id: case
    expected:
      - id: bug
        severity: major
        category: correctness
        file: src/app.py
        line_start: 1
        line_end: true
""",
            [],
            "line_end",
        ),
        (
            """
cases:
  - id: case
    changed_lines: true
""",
            [],
            "changed_lines",
        ),
        (
            """
cases:
  - id: case
    expected:
      - id: bug
        severity: major
        category: correctness
        file: src/app.py
        line_start: 1
""",
            [
                {
                    "severity": "major",
                    "category": "correctness",
                    "file": "src/app.py",
                    "line": True,
                }
            ],
            "line",
        ),
    ],
)
def test_benchmark_cli_rejects_boolean_line_values(
    tmp_path, manifest_text, findings, invalid_field
):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text(manifest_text)
    reviews = tmp_path / "reviews"
    _write_predictions(reviews, "case", findings)

    result = CliRunner().invoke(
        app,
        [
            "benchmark-review",
            "--manifest",
            str(manifest),
            "--reviews",
            str(reviews),
        ],
    )

    assert result.exit_code == 1
    assert "could not score benchmark" in result.output
    assert invalid_field in result.output


def test_benchmark_cli_reports_missing_line_start_without_traceback(tmp_path):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text(
        """
cases:
  - id: missing-line-start
    expected:
      - id: bug
        severity: major
        category: correctness
        file: src/app.py
"""
    )

    result = CliRunner().invoke(
        app,
        [
            "benchmark-review",
            "--manifest",
            str(manifest),
            "--reviews",
            str(tmp_path / "reviews"),
        ],
    )

    assert result.exit_code == 1
    assert "could not score benchmark" in result.output
    assert "line_start" in result.output
    assert "KeyError" not in result.output


def test_manifest_rejects_ambiguous_expected_ranges_after_path_normalization():
    with pytest.raises(ValidationError, match="ambiguous overlapping ranges"):
        BenchmarkCase(
            id="ambiguous",
            expected_findings=[
                _expected(
                    "first",
                    file="src/service.py",
                    line_start=15,
                    line_end=20,
                ),
                _expected(
                    "second",
                    file=".\\src\\service.py",
                    line_start=20,
                    line_end=25,
                ),
            ],
        )

    valid = BenchmarkCase(
        id="unambiguous",
        expected_findings=[
            _expected("first", line_start=15, line_end=19),
            _expected("second", line_start=20, line_end=25),
        ],
    )

    assert len(valid.expected_findings) == 2


def test_duplicate_predictions_and_order_independence(tmp_path):
    manifest = BenchmarkManifest(
        cases=[
            BenchmarkCase(
                id="overlap",
                expected_findings=[
                    _expected(
                        "first",
                        severity="minor",
                        file="src/service.py",
                        line_start=15,
                    ),
                    _expected(
                        "second",
                        severity="major",
                        file="src/service.py",
                        line_start=20,
                    ),
                ],
            )
        ]
    )
    predictions = [
        {
            "severity": "minor",
            "category": "correctness",
            "file": ".\\src\\service.py",
            "line": 15,
        },
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/service.py",
            "line": 20,
        },
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/service.py",
            "line": 20,
        },
    ]

    reviews_a = tmp_path / "a"
    reviews_b = tmp_path / "b"
    _write_predictions(reviews_a, "overlap", predictions)
    _write_predictions(reviews_b, "overlap", list(reversed(predictions)))

    result_a = score_benchmark(manifest, reviews_a)
    result_b = score_benchmark(manifest, reviews_b)

    case = result_a.cases[0]
    assert (case.true_positives, case.false_positives, case.false_negatives) == (
        2,
        1,
        0,
    )
    assert {match.expected_id for match in case.matches} == {"first", "second"}
    assert all(match.severity_correct for match in case.matches)
    assert result_a.metrics.model_dump() == result_b.metrics.model_dump()


def test_metric_math_clean_cases_and_wilson_intervals(tmp_path):
    manifest = BenchmarkManifest(
        cases=[
            BenchmarkCase(
                id="bugs",
                changed_lines=100,
                expected_findings=[
                    _expected(
                        "security",
                        severity="blocker",
                        category="security",
                        line_start=10,
                    ),
                    _expected("logic", severity="major", line_start=20, line_end=25),
                    _expected(
                        "test-gap",
                        severity="minor",
                        category="tests",
                        file="tests/test_app.py",
                        line_start=5,
                    ),
                ],
            ),
            BenchmarkCase(id="clean", changed_lines=0),
        ]
    )
    reviews = tmp_path / "reviews"
    _write_predictions(
        reviews,
        "bugs",
        [
            {
                "severity": "blocker",
                "category": "security",
                "file": "src/app.py",
                "line": 10,
            },
            {
                "severity": "minor",
                "category": "correctness",
                "file": "src/app.py",
                "line": 22,
                "confidence": 0.7,
            },
            {
                "severity": "nit",
                "category": "style",
                "file": "src/app.py",
                "line": 30,
            },
        ],
    )
    _write_predictions(reviews, "clean", [])

    result = score_benchmark(manifest, reviews)
    metrics = result.metrics

    assert metrics.case_count == 2
    assert (metrics.tp, metrics.fp, metrics.fn) == (2, 1, 1)
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.recall == pytest.approx(2 / 3)
    assert metrics.f1 == pytest.approx(2 / 3)
    assert metrics.blocker_major_recall == 1.0
    assert metrics.severity_accuracy == 0.5
    assert metrics.false_positives_per_case == 0.5
    assert metrics.false_positives_per_kloc == 10.0
    assert metrics.clean_case_accuracy == 1.0
    assert metrics.clean_case_denominator == 1
    assert metrics.precision_wilson_95 is not None
    assert metrics.precision_wilson_95.lower == pytest.approx(0.2076596008)
    assert metrics.precision_wilson_95.upper == pytest.approx(0.9385080553)
    assert metrics.recall_wilson_95 == metrics.precision_wilson_95
    assert result.cases[0].prediction_results[0].finding.confidence == 1.0
    json.dumps(result.model_dump(mode="json"))


def test_missing_prediction_is_zero_findings_with_status(tmp_path):
    manifest = BenchmarkManifest(
        cases=[
            BenchmarkCase(
                id="missing",
                expected_findings=[_expected("bug")],
            )
        ]
    )

    result = score_benchmark(manifest, tmp_path / "no-reviews")
    case = result.cases[0]
    metrics = result.metrics

    assert case.status == "missing_prediction"
    assert case.note is not None and "zero findings" in case.note
    assert (case.tp, case.fp, case.fn) == (0, 0, 1)
    assert metrics.precision is None
    assert metrics.precision_denominator == 0
    assert metrics.precision_wilson_95 is None
    assert metrics.recall == 0.0
    assert metrics.recall_denominator == 1
    assert metrics.f1 == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"findings": None},
        {"llm": None},
        {"llm": {}},
    ],
)
def test_incomplete_findings_payload_is_not_a_completed_clean_review(tmp_path, payload):
    manifest = BenchmarkManifest(cases=[BenchmarkCase(id="clean", changed_lines=1)])
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "clean.json").write_text(json.dumps(payload))

    result = score_benchmark(manifest, reviews)
    case = result.cases[0]

    assert case.status == "incomplete_prediction"
    assert case.note is not None
    assert case.prediction_count == 0
    assert result.metrics.clean_case_denominator == 1
    assert result.metrics.clean_cases_correct == 0
    assert result.metrics.clean_case_accuracy == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        {
            "findings": [
                {
                    "category": "correctness",
                    "file": "src/app.py",
                    "line": 10,
                }
            ]
        },
        {
            "llm": {
                "findings": [
                    {
                        "category": "correctness",
                        "file": "src/app.py",
                        "line": 10,
                        "verified": True,
                        "verdict": "confirmed",
                    }
                ]
            }
        },
    ],
)
def test_prediction_severity_is_required(tmp_path, payload):
    manifest = BenchmarkManifest(cases=[BenchmarkCase(id="missing-severity")])
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "missing-severity.json").write_text(json.dumps(payload))

    with pytest.raises(ValidationError, match="severity"):
        score_benchmark(manifest, reviews)


def test_native_change_report_filters_inactive_findings(tmp_path):
    expected = [
        _expected("confirmed", line_start=10),
        _expected("unadjudicated", line_start=20),
        _expected("rejected", line_start=30),
        _expected("unverified", line_start=40),
    ]
    manifest = BenchmarkManifest(
        cases=[BenchmarkCase(id="native", expected_findings=expected)]
    )
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    native_findings = [
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/app.py",
            "line": 10,
            "verified": True,
            "verdict": "confirmed",
        },
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/app.py",
            "line": 20,
            "verified": True,
            "verdict": None,
        },
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/app.py",
            "line": 30,
            "verified": True,
            "verdict": "rejected",
        },
        {
            "severity": "major",
            "category": "correctness",
            "file": "src/app.py",
            "line": 40,
            "verified": False,
            "verdict": "confirmed",
        },
    ]
    (reviews / "native.json").write_text(
        json.dumps(
            {
                "repo": "/repo",
                "base": "main",
                "head": "feature",
                "llm": {"findings": native_findings},
            }
        )
    )

    result = score_benchmark(manifest, reviews)
    case = result.cases[0]

    assert case.status == "scored"
    assert case.prediction_count == 2
    assert (case.tp, case.fp, case.fn) == (2, 0, 2)
    assert {match.expected_id for match in case.matches} == {
        "confirmed",
        "unadjudicated",
    }


def test_clean_case_accuracy_penalizes_generic_false_alarms(tmp_path):
    manifest = BenchmarkManifest(
        cases=[BenchmarkCase(id="quiet"), BenchmarkCase(id="noisy")]
    )
    reviews = tmp_path / "reviews"
    _write_predictions(reviews, "quiet", [])
    _write_predictions(
        reviews,
        "noisy",
        [
            {
                "severity": "minor",
                "category": "correctness",
                "file": "src/app.py",
                "line": 1,
                "verified": False,
                "verdict": "rejected",
            }
        ],
    )

    metrics = score_benchmark(manifest, reviews).metrics

    assert metrics.clean_case_denominator == 2
    assert metrics.clean_cases_correct == 1
    assert metrics.clean_case_accuracy == 0.5
    assert metrics.false_positives == 1


def test_zero_denominators_are_explicit_for_empty_benchmarks(tmp_path):
    result = score_benchmark(BenchmarkManifest(cases=[]), tmp_path)
    metrics = result.metrics

    assert metrics.case_count == 0
    assert metrics.precision is None
    assert metrics.recall is None
    assert metrics.f1 is None
    assert metrics.blocker_major_recall is None
    assert metrics.severity_accuracy is None
    assert metrics.false_positives_per_case is None
    assert metrics.false_positives_per_kloc is None
    assert metrics.clean_case_accuracy is None
    assert metrics.precision_wilson_95 is None
    assert metrics.recall_wilson_95 is None


def test_fp_per_kloc_requires_changed_lines_for_every_case(tmp_path):
    manifest = BenchmarkManifest(
        cases=[
            BenchmarkCase(id="measured", changed_lines=100),
            BenchmarkCase(id="unknown"),
        ]
    )
    reviews = tmp_path / "reviews"
    _write_predictions(
        reviews,
        "measured",
        [
            {
                "severity": "minor",
                "category": "correctness",
                "file": "src/app.py",
                "line": 1,
            }
        ],
    )
    _write_predictions(reviews, "unknown", [])

    metrics = score_benchmark(manifest, reviews).metrics

    assert metrics.false_positives == 1
    assert metrics.changed_lines is None
    assert metrics.false_positives_per_kloc is None


def test_benchmark_cli_writes_item_results_and_enforces_regression_gates(tmp_path):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text(
        """
cases:
  - id: auth-bypass
    changed_lines: 20
    expected:
      - id: auth-1
        severity: blocker
        category: security
        file: src/auth.py
        line_start: 12
"""
    )
    reviews = tmp_path / "reviews"
    _write_predictions(
        reviews,
        "auth-bypass",
        [
            {
                "severity": "blocker",
                "category": "security",
                "file": "src/auth.py",
                "line": 12,
            },
            {
                "severity": "minor",
                "category": "style",
                "file": "src/auth.py",
                "line": 15,
            },
        ],
    )
    output = tmp_path / "result.json"
    runner = CliRunner()

    passed = runner.invoke(
        app,
        [
            "benchmark-review",
            "--manifest",
            str(manifest),
            "--reviews",
            str(reviews),
            "--out",
            str(output),
            "--min-recall",
            "1",
            "--max-fp-per-case",
            "1",
        ],
    )
    failed = runner.invoke(
        app,
        [
            "benchmark-review",
            "--manifest",
            str(manifest),
            "--reviews",
            str(reviews),
            "--min-precision",
            "0.75",
        ],
    )

    assert passed.exit_code == 0, passed.output
    assert json.loads(output.read_text())["metrics"]["precision"] == 0.5
    assert failed.exit_code == 2
    assert "gate failed: precision 0.500" in failed.output


def test_benchmark_cli_fails_closed_on_missing_outputs(tmp_path):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text("cases:\n  - id: missing-clean-control\n")
    runner = CliRunner()
    args = [
        "benchmark-review",
        "--manifest",
        str(manifest),
        "--reviews",
        str(tmp_path / "reviews"),
    ]

    failed = runner.invoke(app, args)
    allowed = runner.invoke(app, [*args, "--allow-missing"])

    assert failed.exit_code == 2
    assert "no reviewer output" in failed.output
    assert allowed.exit_code == 0


def test_benchmark_cli_fails_closed_on_incomplete_outputs(tmp_path):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text("cases:\n  - id: incomplete-clean-control\n")
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    (reviews / "incomplete-clean-control.json").write_text(json.dumps({"llm": None}))
    runner = CliRunner()
    args = [
        "benchmark-review",
        "--manifest",
        str(manifest),
        "--reviews",
        str(reviews),
    ]

    failed = runner.invoke(app, args)
    allowed = runner.invoke(app, [*args, "--allow-missing"])

    assert failed.exit_code == 2
    assert "incomplete" in failed.output
    assert "findings payload" in failed.output
    assert allowed.exit_code == 0


@pytest.mark.parametrize(
    "option",
    [
        "--min-precision",
        "--min-recall",
        "--min-critical-recall",
        "--max-fp-per-case",
    ],
)
def test_benchmark_cli_rejects_nonfinite_thresholds(tmp_path, option):
    manifest = tmp_path / "benchmark.yaml"
    manifest.write_text("cases:\n  - id: clean\n")
    reviews = tmp_path / "reviews"
    _write_predictions(reviews, "clean", [])

    result = CliRunner().invoke(
        app,
        [
            "benchmark-review",
            "--manifest",
            str(manifest),
            "--reviews",
            str(reviews),
            option,
            "nan",
        ],
    )

    assert result.exit_code == 2
    assert f"Invalid value for {option}" in result.output
    assert "must be finite" in result.output
