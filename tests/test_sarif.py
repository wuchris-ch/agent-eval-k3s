import json

from agent_eval.metrics import ScanResults
from agent_eval.review import (
    ChangedFile,
    ChangeReport,
    Finding,
    LLMReview,
    _persist,
)
from agent_eval.sarif import to_sarif, write_sarif


def _report(*findings: Finding, scans: ScanResults | None = None) -> ChangeReport:
    return ChangeReport(
        repo="/Users/reviewer/acme",
        base="main",
        head="feature/sarif",
        files=[ChangedFile(path="src/app.py"), ChangedFile(path="src/a file.py")],
        llm=LLMReview(findings=list(findings)),
        scans=scans,
        risk="high",
        blocked=True,
    )


def _results(report: ChangeReport) -> list[dict]:
    return to_sarif(report)["runs"][0]["results"]


def test_only_active_llm_findings_are_emitted_and_severity_is_mapped():
    active = [
        Finding(
            severity=severity,
            category="correctness",
            file="src/app.py",
            line=index + 1,
            claim=f"{severity} issue",
            evidence=f"stable evidence {index}",
            verified=True,
            verdict="confirmed",
        )
        for index, severity in enumerate(("blocker", "major", "minor", "nit"))
    ]
    rejected = Finding(
        severity="blocker",
        file="src/app.py",
        claim="rejected",
        verified=True,
        verdict="rejected",
    )
    unverified = Finding(
        severity="major", file="src/app.py", claim="unverified", verified=False
    )

    results = _results(_report(*active, rejected, unverified))

    assert [result["message"]["text"] for result in results] == [
        "blocker issue",
        "major issue",
        "minor issue",
        "nit issue",
    ]
    assert [result["level"] for result in results] == [
        "error",
        "error",
        "warning",
        "note",
    ]


def test_rule_ids_and_fingerprints_are_stable_across_line_changes():
    first = Finding(
        severity="major",
        category="security",
        file="src/app.py",
        line=4,
        claim="Authorization can be bypassed",
        evidence="return user.is_admin",
        verified=True,
    )
    shifted = first.model_copy(update={"line": 104})

    first_result = _results(_report(first))[0]
    shifted_result = _results(_report(shifted))[0]

    assert first_result["ruleId"] == shifted_result["ruleId"]
    assert first_result["partialFingerprints"] == shifted_result["partialFingerprints"]
    fingerprint = next(iter(first_result["partialFingerprints"].values()))
    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_scanner_findings_require_rule_and_safe_path_and_map_unknown_severity():
    scans = ScanResults(
        findings=[
            {
                "tool": "semgrep",
                "rule": "python.lang.security.audit.exec-used",
                "severity": "ERROR",
                "path": "/tmp/scanner-checkout/src/app.py",
                "line": 9,
                "message": "User-controlled data reaches exec.",
            },
            {
                "tool": "custom",
                "rule": "needs-review",
                "severity": "UNKNOWN",
                "path": "src/app.py",
                "line": 0,
            },
            {
                "tool": "custom",
                "rule": "zero-score",
                "severity": 0,
                "path": "src/app.py",
                "line": -3,
            },
            {"tool": "semgrep", "path": "src/app.py", "line": 2},
            {"tool": "semgrep", "rule": "missing-path"},
            {
                "tool": "semgrep",
                "rule": "outside-repository",
                "path": "/private/secrets.py",
            },
        ]
    )

    results = _results(_report(scans=scans))

    assert [result["level"] for result in results] == ["error", "note", "note"]
    assert [result["properties"]["source"] for result in results] == [
        "scanner",
        "scanner",
        "scanner",
    ]
    assert results[0]["locations"][0]["physicalLocation"]["artifactLocation"][
        "uri"
    ] == "src/app.py"
    assert results[0]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 9
    }
    assert "region" not in results[1]["locations"][0]["physicalLocation"]
    assert "region" not in results[2]["locations"][0]["physicalLocation"]


def test_locations_are_relative_encoded_and_only_contain_positive_lines():
    encoded = Finding(
        file="/Users/reviewer/acme/src/a file.py",
        line=0,
        claim="Encoded path",
        evidence="some code",
        verified=True,
    )
    positive = Finding(
        file="src/app.py",
        line=7,
        claim="Positive line",
        evidence="other code",
        verified=True,
    )
    external = Finding(
        file="/Users/reviewer/private/token.txt",
        line=5,
        claim="No safe repository location",
        evidence="token",
        verified=True,
    )

    document = to_sarif(_report(encoded, positive, external))
    results = document["runs"][0]["results"]

    encoded_location = results[0]["locations"][0]["physicalLocation"]
    assert encoded_location["artifactLocation"]["uri"] == "src/a%20file.py"
    assert "region" not in encoded_location
    assert results[1]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 7
    }
    assert "locations" not in results[2]
    artifact_uris = [
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for result in results
        if "locations" in result
    ]
    assert all(not uri.startswith("/") for uri in artifact_uris)
    assert "/Users/reviewer" not in json.dumps(document)


def test_invocation_metadata_and_serialization(tmp_path):
    report = _report(
        Finding(
            file="src/app.py",
            line=1,
            claim="Unicode is preserved: café",
            evidence="café",
            verified=True,
        )
    )
    output = tmp_path / "nested" / "review.sarif"

    returned = write_sarif(report, output)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert returned == output
    assert loaded == to_sarif(report)
    assert output.read_bytes().endswith(b"\n")
    assert loaded["version"] == "2.1.0"
    assert loaded["$schema"].endswith("sarif-schema-2.1.0.json")
    invocation = loaded["runs"][0]["invocations"][0]
    assert invocation == {
        "executionSuccessful": False,
        "properties": {
            "base": "main",
            "head": "feature/sarif",
            "overallRisk": "high",
            "blocked": True,
        },
    }
    rules = loaded["runs"][0]["tool"]["driver"]["rules"]
    result = loaded["runs"][0]["results"][0]
    assert rules[result["ruleIndex"]]["id"] == result["ruleId"]


def test_change_report_persistence_always_includes_sarif(tmp_path):
    report = _report(
        Finding(
            file="src/app.py",
            line=2,
            claim="Persisted finding",
            evidence="return unsafe_value",
            verified=True,
        )
    )

    _persist(report, tmp_path)

    assert (tmp_path / "review.json").is_file()
    assert (tmp_path / "review.md").is_file()
    sarif = json.loads((tmp_path / "review.sarif").read_text())
    assert sarif["runs"][0]["results"][0]["message"]["text"] == "Persisted finding"
