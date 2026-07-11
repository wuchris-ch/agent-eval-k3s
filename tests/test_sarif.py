import json
from pathlib import Path
from types import SimpleNamespace

from agent_eval.evaluators import scanners
from agent_eval.metrics import ScanResults
from agent_eval.review import (
    ChangedFile,
    ChangeReport,
    Finding,
    LLMReview,
    _parse_head_line_ranges,
    _persist,
    _scope_scans_to_changed_lines,
)
from agent_eval.sarif import to_sarif, write_sarif


def _report(*findings: Finding, scans: ScanResults | None = None) -> ChangeReport:
    return ChangeReport(
        repo="/Users/reviewer/acme",
        base="main",
        head="feature/sarif",
        files=[
            ChangedFile(path="src/app.py", head_line_ranges=[(1, 20)]),
            ChangedFile(path="src/a file.py", head_line_ranges=[(1, 20)]),
        ],
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
    unconfirmed = Finding(
        severity="blocker",
        file="src/app.py",
        line=5,
        claim="unconfirmed",
        verified=True,
    )

    results = _results(_report(*active, rejected, unverified, unconfirmed))

    assert [result["message"]["text"] for result in results] == [
        "blocker issue",
        "major issue",
        "minor issue",
        "nit issue",
        "unconfirmed",
    ]
    assert [result["level"] for result in results] == [
        "error",
        "error",
        "warning",
        "note",
        "warning",
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
        verdict="confirmed",
    )
    shifted = first.model_copy(update={"line": 14, "severity": "minor"})

    first_result = _results(_report(first))[0]
    shifted_result = _results(_report(shifted))[0]

    assert first_result["ruleId"] == shifted_result["ruleId"]
    assert first_result["partialFingerprints"] == shifted_result["partialFingerprints"]
    fingerprints = first_result["partialFingerprints"]
    assert set(fingerprints) == {
        "primaryLocationLineHash",
        "agentEvalSemanticIdentity/v2",
    }
    assert fingerprints["primaryLocationLineHash"].endswith(":1")
    internal = fingerprints["agentEvalSemanticIdentity/v2"]
    assert len(internal) == 64
    assert all(character in "0123456789abcdef" for character in internal)


def test_fingerprints_distinguish_claims_sharing_evidence():
    common = {
        "severity": "major",
        "category": "security",
        "file": "src/app.py",
        "evidence": "return user.is_admin",
        "verified": True,
        "verdict": "confirmed",
    }

    first, second = _results(_report(
        Finding(line=4, claim="Authorization can be bypassed", **common),
        Finding(line=12, claim="Audit logging can be bypassed", **common),
    ))

    assert (
        first["partialFingerprints"]["primaryLocationLineHash"]
        != second["partialFingerprints"]["primaryLocationLineHash"]
    )
    assert (
        first["partialFingerprints"]["agentEvalSemanticIdentity/v2"]
        != second["partialFingerprints"]["agentEvalSemanticIdentity/v2"]
    )


def test_identical_llm_fingerprints_count_location_ordered_occurrences():
    common = {
        "severity": "major",
        "category": "security",
        "file": "src/app.py",
        "claim": "Authorization can be bypassed",
        "evidence": "return user.is_admin",
        "verified": True,
        "verdict": "confirmed",
    }
    later = Finding(line=12, **common)
    earlier = Finding(line=4, **common)
    rejected = Finding(line=2, **{**common, "verdict": "rejected"})
    unchanged = Finding(line=21, **common)

    first = _results(_report(later, rejected, unchanged, earlier))
    reordered = _results(_report(earlier, later))

    def fingerprints_by_line(results):
        return {
            result["locations"][0]["physicalLocation"]["region"]["startLine"]:
                result["partialFingerprints"]["primaryLocationLineHash"]
            for result in results
        }

    fingerprints = fingerprints_by_line(first)
    assert fingerprints == fingerprints_by_line(reordered)
    assert fingerprints[4].endswith(":1")
    assert fingerprints[12].endswith(":2")
    assert fingerprints[4].rsplit(":", 1)[0] == fingerprints[12].rsplit(":", 1)[0]


def test_llm_findings_are_limited_to_changed_positive_lines():
    common = {
        "file": "src/app.py",
        "evidence": "return user.is_admin",
        "verified": True,
        "verdict": "confirmed",
    }

    results = _results(_report(
        Finding(line=20, claim="Changed line", **common),
        Finding(line=None, claim="Missing line", **common),
        Finding(line=0, claim="Invalid line", **common),
        Finding(line=21, claim="Unchanged line", **common),
    ))

    assert [result["message"]["text"] for result in results] == ["Changed line"]
    assert results[0]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 20
    }
    assert results[0]["properties"]["diffScoped"] is True


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
                "line": 10,
            },
            {
                "tool": "custom",
                "rule": "zero-score",
                "severity": 0,
                "path": "src/app.py",
                "line": 11,
            },
            {"tool": "semgrep", "path": "src/app.py", "line": 2},
            {"tool": "semgrep", "rule": "missing-path"},
            {
                "tool": "semgrep",
                "rule": "unchanged-line",
                "path": "src/app.py",
                "line": 21,
            },
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
    assert results[1]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 10
    }
    assert results[2]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 11
    }
    assert all(
        "primaryLocationLineHash" in result["partialFingerprints"]
        for result in results
    )


def test_scanner_fingerprints_disambiguate_distinct_results_at_same_location():
    common = {
        "tool": "semgrep",
        "rule": "python.lang.security.audit.exec-used",
        "severity": "ERROR",
        "path": "src/app.py",
        "line": 9,
    }
    findings = [
        {**common, "message": "User-controlled data reaches exec."},
        {**common, "message": "Untrusted data reaches exec."},
    ]

    first = _results(_report(scans=ScanResults(findings=findings)))
    reordered = _results(_report(scans=ScanResults(findings=findings[::-1])))

    def primary_by_message(results):
        return {
            result["message"]["text"]:
                result["partialFingerprints"]["primaryLocationLineHash"]
            for result in results
        }

    fingerprints = primary_by_message(first)
    assert fingerprints == primary_by_message(reordered)
    assert len(set(fingerprints.values())) == 2
    assert {
        fingerprint.rsplit(":", 1)[1]
        for fingerprint in fingerprints.values()
    } == {"1", "2"}
    assert len({
        fingerprint.rsplit(":", 1)[0]
        for fingerprint in fingerprints.values()
    }) == 1


def test_locations_are_relative_encoded_and_only_contain_positive_lines():
    encoded = Finding(
        file="/Users/reviewer/acme/src/a file.py",
        line=6,
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
    shorthand = Finding(
        file="app.py",
        line=8,
        claim="Shorthand path",
        evidence="changed code",
        verified=True,
    )
    external = Finding(
        file="/Users/reviewer/private/token.txt",
        line=5,
        claim="No safe repository location",
        evidence="token",
        verified=True,
    )
    remote = Finding(
        file="https://example.test/src/app.py",
        line=9,
        claim="Remote path",
        evidence="changed code",
        verified=True,
    )
    traversal = Finding(
        file="../checkout/src/app.py",
        line=10,
        claim="Traversal path",
        evidence="changed code",
        verified=True,
    )

    document = to_sarif(_report(
        encoded,
        positive,
        shorthand,
        external,
        remote,
        traversal,
    ))
    results = document["runs"][0]["results"]

    encoded_location = results[0]["locations"][0]["physicalLocation"]
    assert encoded_location["artifactLocation"]["uri"] == "src/a%20file.py"
    assert encoded_location["region"] == {"startLine": 6}
    assert results[1]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 7
    }
    assert results[2]["locations"][0]["physicalLocation"]["artifactLocation"] == {
        "uri": "src/app.py"
    }
    artifact_uris = [
        result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for result in results
        if "locations" in result
    ]
    assert all(not uri.startswith("/") for uri in artifact_uris)
    assert "/Users/reviewer" not in json.dumps(document)


def test_ambiguous_shorthand_path_is_not_emitted():
    report = _report(Finding(
        file="app.py",
        line=3,
        claim="Ambiguous path",
        evidence="changed code",
        verified=True,
    ))
    report.files.append(ChangedFile(
        path="tests/app.py",
        head_line_ranges=[(1, 10)],
    ))

    assert _results(report) == []


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
        "executionSuccessful": True,
        "properties": {
            "base": "main",
            "head": "feature/sarif",
            "overallRisk": "high",
            "blocked": True,
            "analysisScope": "pr-diff",
        },
    }
    rules = loaded["runs"][0]["tool"]["driver"]["rules"]
    result = loaded["runs"][0]["results"][0]
    assert rules[result["ruleIndex"]]["id"] == result["ruleId"]


def test_diff_parser_tracks_only_added_head_lines():
    diff = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,5 @@
 context
-old
+new
+++ increment
 context
+tail
"""

    ranges = _parse_head_line_ranges(diff, ["src/app.py"])

    assert ranges == {"src/app.py": [(2, 3), (5, 5)]}


def test_review_scanner_counts_and_findings_are_diff_scoped():
    files = [ChangedFile(path="src/app.py", head_line_ranges=[(2, 3)])]
    scans = ScanResults(
        sec_findings_high=2,
        sec_findings_medium=0,
        sec_findings_low=0,
        secrets_found=2,
        findings=[
            {
                "tool": "semgrep",
                "rule": "dangerous-call",
                "severity": "ERROR",
                "path": "src/app.py",
                "line": 2,
            },
            {
                "tool": "semgrep",
                "rule": "pre-existing-dangerous-call",
                "severity": "ERROR",
                "path": "src/app.py",
                "line": 8,
            },
            {
                "tool": "gitleaks",
                "rule": "generic-api-key",
                "severity": "ERROR",
                "path": "/tmp/review/src/app.py",
                "line": 3,
            },
            {
                "tool": "gitleaks",
                "rule": "old-secret",
                "severity": "ERROR",
                "path": "/tmp/review/src/app.py",
                "line": 9,
            },
        ],
    )

    _scope_scans_to_changed_lines(scans, files)

    assert scans.sec_findings_high == 1
    assert scans.secrets_found == 1
    assert [(finding["tool"], finding["line"]) for finding in scans.findings] == [
        ("semgrep", 2),
        ("gitleaks", 3),
    ]


def test_gitleaks_retains_redacted_item_locations(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    scans_dir = tmp_path / "scans"
    workspace.mkdir()
    scans_dir.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text(
        'one\ntwo\napi_key = "do-not-persist-this"\n'
    )

    def fake_run(cmd, out_file):
        del out_file
        report = Path(cmd[cmd.index("--report-path") + 1])
        report.write_text(json.dumps([{
            "RuleID": "generic-api-key",
            "Description": "Generic API key",
            "File": "src/app.py",
            "StartLine": 3,
            "Match": 'api_key = "do-not-persist-this"',
        }]))
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._gitleaks(workspace, scans_dir, results)

    assert results.secrets_found == 1
    assert len(results.findings) == 1
    finding = results.findings[0]
    assert finding == {
        "tool": "gitleaks",
        "rule": "generic-api-key",
        "severity": "ERROR",
        "path": "src/app.py",
        "line": 3,
        "primary_location_line_hash": finding["primary_location_line_hash"],
        "semantic_location_hash": finding["semantic_location_hash"],
    }
    assert finding["primary_location_line_hash"] == scanners._line_fingerprint(
        ["one", "two", "<REDACTED_SECRET>"], 3
    )
    assert len(finding["semantic_location_hash"].split(":", 1)[0]) == 64
    assert "Secret" not in finding
    assert "Match" not in finding
    assert "do-not-persist-this" not in results.model_dump_json()
    retained_report = (scans_dir / "gitleaks.json").read_text()
    assert json.loads(retained_report) == results.findings
    assert "do-not-persist-this" not in retained_report
    retained_log = (scans_dir / "gitleaks.log").read_text()
    assert retained_log == "exit_code=1 redacted_findings=1\n"
    assert "do-not-persist-this" not in retained_log
    sarif_result = _results(_report(scans=results))[0]
    assert sarif_result["properties"]["tool"] == "gitleaks"
    assert (
        sarif_result["partialFingerprints"]["primaryLocationLineHash"]
        == finding["primary_location_line_hash"]
    )
    assert "do-not-persist-this" not in json.dumps(sarif_result)


def test_redacted_gitleaks_fingerprints_survive_shifts_and_count_duplicates(
    tmp_path,
):
    source = tmp_path / "app.py"
    source.write_text(
        'api_key = "first-private-value"\n'
        'safe = True\n'
        'api_key = "second-private-value"\n'
    )
    findings = [
        {
            "File": "app.py",
            "StartLine": 1,
            "Secret": "first-private-value",
            "Match": 'api_key = "first-private-value"',
        },
        {
            "File": "app.py",
            "StartLine": 3,
            "Secret": "second-private-value",
            "Match": 'api_key = "second-private-value"',
        },
    ]

    before = scanners._redacted_gitleaks_fingerprints(tmp_path, findings)
    before_identities = scanners._redacted_gitleaks_identities(tmp_path, findings)
    source.write_text(
        'unrelated = True\n'
        'api_key = "first-private-value"\n'
        'safe = True\n'
        'api_key = "second-private-value"\n'
    )
    shifted_findings = [
        {**findings[0], "StartLine": 2},
        {**findings[1], "StartLine": 4},
    ]
    after = scanners._redacted_gitleaks_fingerprints(
        tmp_path, shifted_findings
    )
    after_identities = scanners._redacted_gitleaks_identities(
        tmp_path, shifted_findings
    )

    assert before == after
    assert before_identities == after_identities
    assert before[0].endswith(":1")
    assert before[1].endswith(":2")
    assert before_identities[0][1] != before_identities[1][1]

    def sarif_identity(line, identity):
        scans = ScanResults(findings=[{
            "tool": "gitleaks",
            "rule": "generic-api-key",
            "severity": "ERROR",
            "path": "src/app.py",
            "line": line,
            "primary_location_line_hash": identity[0],
            "semantic_location_hash": identity[1],
        }])
        return _results(_report(scans=scans))[0]["partialFingerprints"][
            "agentEvalSemanticIdentity/v2"
        ]

    assert sarif_identity(1, before_identities[0]) == sarif_identity(
        2, after_identities[0]
    )
    assert sarif_identity(1, before_identities[0]) != sarif_identity(
        3, before_identities[1]
    )


def test_redacted_gitleaks_fingerprint_requires_redaction_at_reported_line(
    tmp_path,
):
    source = tmp_path / "app.py"
    source.write_text(
        'api_key = "private-value"\n'
        'encoded_key = "different-value"\n'
    )
    findings = [{
        "File": "app.py",
        "StartLine": 2,
        "Secret": "private-value",
        "Match": 'encoded_key = "different-value"',
    }]

    assert scanners._redacted_gitleaks_fingerprints(tmp_path, findings) == {}


def test_source_line_fingerprint_is_stable_across_line_shifts(tmp_path):
    source = tmp_path / "app.py"
    source.write_text("dangerous()\nsafe()\ndangerous()\n")

    first = scanners._source_line_fingerprint(tmp_path, source, 1)
    repeated = scanners._source_line_fingerprint(tmp_path, source, 3)
    source.write_text("inserted()\ndangerous()\nsafe()\ndangerous()\n")
    shifted = scanners._source_line_fingerprint(tmp_path, source, 2)

    assert first == shifted
    assert first.endswith(":1")
    assert repeated.endswith(":2")


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
