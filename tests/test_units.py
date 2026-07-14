import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_eval.agents.claude_code import ClaudeCodeAdapter
from agent_eval.evaluators.tests import parse_coverage, parse_junit
from agent_eval.report import pass_at_k
from agent_eval.task import load_task

REPO = Path(__file__).resolve().parents[1]


def test_load_example_task():
    task = load_task("example-todo-api")
    assert task.image_tag.startswith("agent-eval/example-todo-api:")
    assert task.image_tag != "agent-eval/example-todo-api:latest"
    assert len(task.image_tag.rsplit(":", 1)[1]) == 12
    assert "junit.xml" in task.test_command
    assert task.validate_layout() == []
    assert abs(sum(task.judge.weights.values()) - 1.0) < 1e-9


def test_task_image_tag_changes_with_build_context(tmp_path):
    from agent_eval.task import load_task

    task_dir = tmp_path / "content-task"
    workspace = task_dir / "environment" / "workspace"
    tests = task_dir / "tests"
    workspace.mkdir(parents=True)
    tests.mkdir()
    (task_dir / "task.yaml").write_text(
        "id: content-task\nprompt: Change it\ntest_command: pytest\n"
    )
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12\n")
    source = workspace / "app.py"
    source.write_text("value = 1\n")
    (tests / "test_app.py").write_text("def test_x(): assert True\n")
    first = load_task("content-task", tmp_path).image_tag

    source.write_text("value = 2\n")
    second = load_task("content-task", tmp_path).image_tag

    assert first != second


def test_task_image_tag_binds_file_modes_and_generated_context_files(tmp_path):
    task_dir = tmp_path / "content-task"
    workspace = task_dir / "environment" / "workspace"
    tests = task_dir / "tests"
    workspace.mkdir(parents=True)
    tests.mkdir()
    (task_dir / "task.yaml").write_text(
        "id: content-task\nprompt: Change it\ntest_command: pytest\n"
    )
    (task_dir / "environment" / "Dockerfile").write_text("FROM python:3.12\n")
    source = workspace / "tool.py"
    source.write_text("value = 1\n")
    (tests / "test_app.py").write_text("def test_x(): assert True\n")

    original = load_task("content-task", tmp_path).image_tag
    source.chmod(0o755)
    executable = load_task("content-task", tmp_path).image_tag
    assert executable != original

    cache = workspace / "__pycache__"
    cache.mkdir()
    (cache / "tool.pyc").write_bytes(b"generated")
    with_generated_file = load_task("content-task", tmp_path).image_tag
    assert with_generated_file != executable


def test_parse_junit(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="0">'
        '<testcase classname="t" name="ok"/>'
        '<testcase classname="t" name="ok2"/>'
        '<testcase classname="t" name="bad"><failure>boom</failure></testcase>'
        "</testsuite></testsuites>"
    )
    r = parse_junit(junit)
    assert (r.total, r.passed, r.failed) == (3, 2, 1)
    assert r.failures == ["t::bad"]
    assert not r.resolved


def test_parse_junit_missing_is_infra_error(tmp_path):
    r = parse_junit(tmp_path / "nope.xml")
    assert r.infra_error and not r.resolved


@pytest.mark.parametrize(
    ("contents", "error"),
    [
        ("<not-junit/>", "root must be"),
        ('<testsuite tests="many"/>', "non-negative integer"),
        ('<testsuite tests="-1"/>', "non-negative integer"),
        (
            '<testsuite tests="1" failures="1" errors="1" skipped="0"/>',
            "exceed tests count",
        ),
        (
            '<testsuites tests="2"><testsuite tests="1"/></testsuites>',
            "does not match",
        ),
    ],
)
def test_parse_junit_rejects_invalid_structure_and_counts(
    tmp_path, contents, error
):
    junit = tmp_path / "junit.xml"
    junit.write_text(contents)

    result = parse_junit(junit, command_exit_code=0)

    assert result.infra_error and error in result.infra_error
    assert not result.resolved


def test_parse_junit_fails_closed_for_unreadable_or_unicode_invalid_artifacts(
    monkeypatch, tmp_path
):
    junit = tmp_path / "junit.xml"
    junit.write_bytes(b'\xff\xfe<testsuite tests="1"/>')
    invalid_encoding = parse_junit(junit, command_exit_code=0)
    assert invalid_encoding.infra_error
    assert not invalid_encoding.resolved

    def unreadable(_path):
        raise PermissionError("untrusted artifact is not readable")

    monkeypatch.setattr("agent_eval.evaluators.tests.ET.parse", unreadable)
    unreadable_result = parse_junit(junit, command_exit_code=0)
    assert unreadable_result.infra_error == "junit xml unreadable: PermissionError"
    assert not unreadable_result.resolved


def test_parse_junit_fails_closed_for_unknown_xml_encoding(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_bytes(
        b'<?xml version="1.0" encoding="x-unknown"?>'
        b'<testsuite tests="1"/>'
    )

    result = parse_junit(junit, command_exit_code=0)

    assert result.infra_error == "junit xml unreadable: LookupError"
    assert not result.resolved


def test_parse_junit_requires_successful_command_and_a_passed_test(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="t" name="ok"/>'
        "</testsuite></testsuites>"
    )

    assert parse_junit(junit, command_exit_code=0).resolved
    failed_command = parse_junit(junit, command_exit_code=1)
    assert failed_command.command_exit_code == 1
    assert not failed_command.resolved

    junit.write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="1">'
        '<testcase classname="t" name="skipped"><skipped/></testcase>'
        "</testsuite></testsuites>"
    )
    all_skipped = parse_junit(junit, command_exit_code=0)
    assert all_skipped.passed == 0
    assert not all_skipped.resolved


def test_parse_coverage(tmp_path):
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"totals": {"percent_covered": 87.5}}))
    assert parse_coverage(cov) == 87.5
    assert parse_coverage(tmp_path / "nope.json") is None


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json",
        b"\xff\xfe{not utf-8}",
        b'{"totals": {"percent_covered": true}}',
        b'{"totals": {"percent_covered": "100"}}',
        b'{"totals": {"percent_covered": -0.1}}',
        b'{"totals": {"percent_covered": 100.1}}',
        b'{"totals": {"percent_covered": NaN}}',
        b'{"totals": {"percent_covered": ' + (b"9" * 400) + b"}}",
    ],
)
def test_parse_coverage_fails_closed_for_untrusted_artifacts(tmp_path, payload):
    coverage = tmp_path / "coverage.json"
    coverage.write_bytes(payload)

    assert parse_coverage(coverage) is None


def test_parse_coverage_fails_closed_when_artifact_becomes_unreadable(
    monkeypatch, tmp_path
):
    coverage = tmp_path / "coverage.json"
    coverage.write_text('{"totals": {"percent_covered": 100}}')

    def unreadable(_path):
        raise PermissionError("untrusted artifact is not readable")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert parse_coverage(coverage) is None


def test_claude_transcript_parsing(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    events = [
        {"type": "system", "subtype": "init", "model": "claude-haiku-4-5"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "editing"},
            {"type": "tool_use", "name": "Edit", "input": {}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "result", "subtype": "success", "num_turns": 4,
         "total_cost_usd": 0.0123,
         "usage": {"input_tokens": 1000, "output_tokens": 250}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events))
    m = ClaudeCodeAdapter().parse_transcript(transcript)
    assert m.model == "claude-haiku-4-5"
    assert m.tool_calls == 2
    assert m.turns == 4
    assert m.cost_usd == 0.0123
    assert (m.tokens_in, m.tokens_out) == (1000, 250)


def test_claude_command_shape():
    cmd = ClaudeCodeAdapter().build_command(model="claude-haiku-4-5")
    assert "--output-format stream-json" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "claude-haiku-4-5" in cmd


def test_codex_transcript_parsing(tmp_path):
    from agent_eval.agents.codex import CodexAdapter

    transcript = tmp_path / "transcript.jsonl"
    events = [
        {"type": "thread.started", "thread_id": "t1"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
        {"type": "item.completed", "item": {"type": "file_change"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 900, "cached_input_tokens": 100,
                                             "output_tokens": 200}},
    ]
    transcript.write_text("\n".join(json.dumps(e) for e in events))
    m = CodexAdapter().parse_transcript(transcript)
    assert m.tool_calls == 2
    assert m.turns == 1
    assert (m.tokens_in, m.tokens_out) == (900, 200)
    assert m.cost_usd is None


def test_codex_command_shape():
    from agent_eval.agents.codex import CodexAdapter

    cmd = CodexAdapter().build_command(model="gpt-5.1-codex-mini")
    assert "--json" in cmd and "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "-m gpt-5.1-codex-mini" in cmd


def test_pass_at_k():
    assert pass_at_k(1, 1, 1) == 1.0
    assert pass_at_k(3, 0, 1) == 0.0
    assert abs(pass_at_k(3, 1, 1) - 1 / 3) < 1e-9
    assert pass_at_k(3, 1, 3) == 1.0


def test_classify_subsystem():
    from agent_eval.review import classify_subsystem

    assert classify_subsystem("src/auth/login.py") == "auth/security"
    assert classify_subsystem("tests/test_auth.py") == "tests"
    assert classify_subsystem("app/spec/user.spec.ts") == "tests"
    assert classify_subsystem("migrations/0002_add_priority.sql") == "data/migrations"
    assert classify_subsystem("pyproject.toml") == "dependencies"
    assert classify_subsystem(".github/workflows/ci.yml") == "ci/infra"
    assert classify_subsystem("Dockerfile") == "ci/infra"
    assert classify_subsystem("README.md") == "docs"
    assert classify_subsystem("src/todo/service.py") == "app code"


def test_compute_risk_levels():
    from agent_eval.metrics import DiffStats, ScanResults
    from agent_eval.review import ChangedFile, TestRun, compute_risk

    docs_only = [ChangedFile(path="README.md", subsystem="docs", lines_added=5)]
    signals, risk = compute_risk(docs_only, DiffStats(files_changed=1, lines_added=5),
                                 None, None)
    assert risk == "low"

    auth_no_tests = [ChangedFile(path="src/auth/login.py", subsystem="auth/security",
                                 lines_added=30)]
    signals, risk = compute_risk(auth_no_tests,
                                 DiffStats(files_changed=1, lines_added=30), None, None)
    assert risk in ("medium", "high")
    assert any("auth" in s for s in signals)
    assert any("no test changes" in s for s in signals)

    secrets = ScanResults(secrets_found=1)
    _, risk = compute_risk(docs_only, DiffStats(files_changed=1), secrets, None)
    assert risk == "high"

    failing = TestRun(command="pytest", exit_code=1, passed=False)
    _, risk = compute_risk(docs_only, DiffStats(files_changed=1), None, failing)
    assert risk == "high"


def test_max_risk():
    from agent_eval.review import _max_risk

    assert _max_risk("low", "medium") == "medium"
    assert _max_risk("high", "low") == "high"
    assert _max_risk("low", "bogus") == "low"


def test_scope_graders():
    from agent_eval.graders import ReviewPolicy, scope_graders

    policy = ReviewPolicy(blocked_paths=[".github/workflows/*"],
                          allowed_paths=["src/*", "tests/*"],
                          max_files=2, max_lines=100,
                          require_tests_for=["src/*"])
    changed = [("src/app.py", "app code"), ("infra/main.tf", "ci/infra")]
    results = {g.name: g for g in scope_graders(changed, 250, policy)}

    assert results["scope: blocked paths"].passed is True
    allowed = results["scope: allowed paths"]
    assert allowed.passed is False and allowed.blocking
    assert "infra/main.tf" in allowed.details
    assert results["scope: at most 2 files"].passed is True
    size = results["scope: at most 100 changed lines"]
    assert size.passed is False and not size.blocking
    tests_req = results["scope: tests required for covered paths"]
    assert tests_req.passed is False

    blocked = {g.name: g for g in scope_graders(
        [(".github/workflows/ci.yml", "ci/infra")], 10,
        ReviewPolicy(blocked_paths=[".github/workflows/*"]))}
    hit = blocked["scope: blocked paths"]
    assert hit.passed is False and hit.blocking


def test_policy_loading(tmp_path):
    from agent_eval.graders import load_policy

    (tmp_path / ".agent-eval.yaml").write_text(
        "review:\n  test_cmd: pytest -q\n  checks: ['ruff check .']\n"
        "  max_files: 30\n")
    policy = load_policy(tmp_path)
    assert policy.test_cmd == "pytest -q"
    assert policy.checks == ["ruff check ."]
    assert policy.max_files == 30
    assert load_policy(tmp_path / "nowhere").test_cmd is None


def test_explicit_policy_path_must_exist_and_parse(tmp_path):
    from agent_eval.graders import load_policy

    missing = tmp_path / "missing.yaml"
    with pytest.raises(RuntimeError, match="not a readable regular file"):
        load_policy(tmp_path, explicit=missing)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("review: [\n")
    with pytest.raises(RuntimeError, match="invalid review policy"):
        load_policy(tmp_path, explicit=malformed)


def test_review_policy_is_loaded_from_trusted_base_and_rejects_typos(tmp_path):
    from pydantic import ValidationError

    from agent_eval.graders import ReviewPolicy, load_policy

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    policy = tmp_path / ".agent-eval.yaml"
    policy.write_text("review:\n  max_files: 3\n")
    subprocess.run(["git", "add", ".agent-eval.yaml"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "policy"], cwd=tmp_path, check=True
    )
    policy.write_text("review:\n  max_files: 999\n")

    assert load_policy(tmp_path, trusted_ref="HEAD").max_files == 3
    with pytest.raises(ValidationError, match="max_fiels"):
        ReviewPolicy.model_validate({"max_fiels": 3})


def test_verify_findings():
    from agent_eval.review import Finding, verify_findings

    diff = ("diff --git a/src/auth.py b/src/auth.py\n"
            "--- a/src/auth.py\n+++ b/src/auth.py\n"
            "@@ -1 +1,2 @@\n"
            " def authenticate(password):\n"
            "+    return hashlib.md5(password.encode()).hexdigest()\n")
    changed = ["src/auth.py"]

    good = Finding(severity="major", file="src/auth.py",
                   claim="weak hash",
                   evidence="hashlib.md5(password.encode()).hexdigest()",
                   evidence_side="head")
    paraphrase = Finding(severity="major", file="src/auth.py",
                         claim="weak hash", evidence="uses an md5 digest call")
    wrong_file = Finding(severity="major", file="src/other.py",
                         claim="weak hash",
                         evidence="hashlib.md5(password.encode()).hexdigest()")
    diff_prefixed = Finding(
        severity="major",
        file="a/src/auth.py",
        claim="weak hash",
        evidence="hashlib.md5(password.encode()).hexdigest()",
    )
    too_short = Finding(severity="nit", file="src/auth.py",
                        claim="x", evidence="return")

    verified = verify_findings([good, paraphrase, wrong_file, diff_prefixed, too_short],
                               diff, changed)
    assert verified[0].verified is True
    assert verified[1].verified is False
    assert verified[2].verified is False
    assert verified[3].verified is False
    assert verified[4].verified is False
    assert verified[0].line == 2


def test_verify_findings_supports_deletion_only_security_regressions():
    from agent_eval.review import Finding, verify_findings

    diff = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -80,3 +80,2 @@ def delete_account(user, account):
-    if not user.is_admin:
-        raise PermissionError("admin required")
     perform_delete(account)
"""
    finding = Finding(
        severity="blocker",
        category="security",
        file="src/auth.py",
        claim="authorization guard was removed",
        evidence="if not user.is_admin: raise PermissionError(\"admin required\")",
        evidence_side="base",
    )

    verified = verify_findings([finding], diff, ["src/auth.py"])[0]

    assert verified.verified is True
    assert verified.evidence_line == 80
    assert verified.line == 80


def test_verify_findings_binds_evidence_to_declared_file():
    from agent_eval.review import Finding, verify_findings

    diff = """\
diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -9,0 +10 @@
+return user.is_admin or user.is_owner
diff --git a/src/billing.py b/src/billing.py
--- a/src/billing.py
+++ b/src/billing.py
@@ -19,0 +20 @@
+return invoice.total_without_discount
"""
    finding = Finding(
        severity="major",
        file="src/billing.py",
        line=20,
        claim="authorization can be bypassed",
        evidence="return user.is_admin or user.is_owner",
    )

    verified = verify_findings(
        [finding], diff, ["src/auth.py", "src/billing.py"]
    )

    assert verified[0].verified is False


def test_verify_findings_prefers_exact_paths_that_start_with_diff_prefixes():
    from agent_eval.review import Finding, verify_findings

    diff = """\
diff --git a/a/foo.py b/a/foo.py
--- a/a/foo.py
+++ b/a/foo.py
@@ -0,0 +1 @@
+return nested_file_result
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -0,0 +1 @@
+return root_file_result
"""
    wrong_file = Finding(
        severity="major",
        file="a/foo.py",
        line=1,
        claim="wrong file",
        evidence="return root_file_result",
    )
    exact_file = Finding(
        severity="major",
        file="a/foo.py",
        line=1,
        claim="exact file",
        evidence="return nested_file_result",
    )

    verified = verify_findings(
        [wrong_file, exact_file], diff, ["a/foo.py", "foo.py"]
    )

    assert verified[0].verified is False
    assert verified[1].verified is True


def test_verify_findings_validates_and_derives_lines_across_hunks():
    from agent_eval.review import Finding, verify_findings

    diff = """\
diff --git a/src/billing.py b/src/billing.py
--- a/src/billing.py
+++ b/src/billing.py
@@ -9,0 +10 @@
+return invoice.total_without_discount
@@ -99,0 +100 @@
+return invoice.total_with_tax
"""
    wrong_hunk = Finding(
        severity="major",
        file="src/billing.py",
        line=100,
        claim="discount is omitted",
        evidence="return invoice.total_without_discount",
    )
    missing_line = Finding(
        severity="major",
        file="src/billing.py",
        claim="tax is included",
        evidence="return invoice.total_with_tax",
    )

    verified = verify_findings(
        [wrong_hunk, missing_line], diff, ["src/billing.py"]
    )

    assert verified[0].verified is False
    assert verified[1].verified is True
    assert verified[1].line == 100


def test_verify_findings_nit_cap():
    from agent_eval.review import MAX_NITS, Finding, verify_findings

    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
            "@@ -0,0 +1 @@\n"
            "+    some perfectly quotable changed line of code here\n")
    nits = [Finding(severity="nit", file="a.py", claim=f"nit {i}",
                    evidence="some perfectly quotable changed line of code here")
            for i in range(MAX_NITS + 3)]
    verified = verify_findings(nits, diff, ["a.py"])
    assert sum(1 for f in verified if f.verified) == MAX_NITS


def test_llm_findings_risk():
    from agent_eval.review import Finding, LLMReview, llm_findings_risk

    confirmed_blocker = Finding(severity="blocker", verified=True,
                                verdict="confirmed")
    rejected_blocker = Finding(severity="blocker", verified=True,
                               verdict="rejected")
    confirmed_major = Finding(severity="major", verified=True,
                              verdict="confirmed")
    unverified = Finding(severity="blocker", verified=False)

    assert llm_findings_risk(LLMReview(risk="high",
                                       findings=[confirmed_blocker])) == "high"
    assert llm_findings_risk(LLMReview(risk="high",
                                       findings=[rejected_blocker,
                                                 unverified])) == "low"
    assert llm_findings_risk(LLMReview(findings=[confirmed_major])) == "high"


def test_unconfirmed_high_severity_finding_is_not_active():
    from agent_eval.review import Finding

    assert not Finding(severity="blocker", verified=True).active
    assert not Finding(severity="major", verified=True).active
    assert Finding(severity="minor", verified=True).active


def test_reverse_test_grader_does_not_credit_an_already_failing_base(
    monkeypatch, tmp_path
):
    from agent_eval import graders

    calls = []

    def fake_run(cmd, cwd, timeout):
        calls.append((cmd, cwd, timeout))
        return 1, "pre-existing base failure"

    monkeypatch.setattr(graders, "_run_shell", fake_run)
    result = graders.reverse_test_grader(
        "pytest -q", tmp_path, {"tests/test_new.py": "def test_new(): pass\n"}
    )

    assert result.passed is None
    assert "already fails" in result.details
    assert len(calls) == 1
    assert not (tmp_path / "tests/test_new.py").exists()


def test_judge_requires_every_configured_dimension_once(monkeypatch, tmp_path):
    from agent_eval.evaluators import judge

    task = load_task("example-todo-api")
    (tmp_path / "workspace.diff").write_text("+changed behavior\n")
    response = judge.JudgeResponse(
        scores=[
            judge.DimensionScore(
                dimension="spec_adherence", score=5, rationale="first"
            ),
            judge.DimensionScore(
                dimension="spec_adherence", score=4, rationale="duplicate"
            ),
            judge.DimensionScore(
                dimension="maintainability", score=4, rationale="present"
            ),
        ]
    )
    monkeypatch.setattr(judge, "pick_backend", lambda: "codex")
    monkeypatch.setattr(
        judge,
        "structured_completion",
        lambda *args, **kwargs: (response, "test-model"),
    )

    result = judge.run_judge(task, tmp_path)

    assert result.weighted_score is None
    assert "incomplete or duplicate" in result.rationale["_error"]
    assert (tmp_path / "judge.json").is_file()


def test_auto_judge_requires_codex_auth_not_only_binary(monkeypatch, tmp_path):
    from agent_eval.evaluators import judge

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_EVAL_JUDGE", "auto")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(judge.shutil, "which", lambda name: "/usr/bin/codex")

    assert judge.pick_backend() is None

    auth = tmp_path / "codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text("{}")
    assert judge.pick_backend() == "codex"


def test_compute_risk_with_graders():
    from agent_eval.graders import GraderResult
    from agent_eval.metrics import DiffStats, ScanResults
    from agent_eval.review import ChangedFile, compute_risk

    docs_only = [ChangedFile(path="README.md", subsystem="docs", lines_added=5)]
    stats = DiffStats(files_changed=1, lines_added=5)

    blocked = GraderResult(name="check: build", category="command",
                           blocking=True, passed=False)
    signals, risk = compute_risk(docs_only, stats, None, None, [blocked])
    assert risk == "high"
    assert any("blocking grader failed" in s for s in signals)

    weak_tests = GraderResult(name="new/changed tests vs base commit",
                              category="reverse-classical", blocking=False,
                              weight=2, passed=False,
                              details="tests also pass on base")
    signals, risk = compute_risk(docs_only, stats, None, None, [weak_tests])
    assert risk == "medium"

    skipped = GraderResult(name="x", category="scope", passed=None)
    _, risk = compute_risk(docs_only, stats, None, None, [skipped])
    assert risk == "low"

    high_scan = ScanResults(sec_findings_high=1)
    _, risk = compute_risk(docs_only, stats, high_scan, None, [])
    assert risk == "high"


def test_scanner_graders_fail_closed_on_missing_or_excess_evidence():
    from agent_eval.graders import ReviewPolicy, scanner_graders
    from agent_eval.metrics import ScanResults

    policy = ReviewPolicy(
        required_scanners=["gitleaks"],
        max_lint_errors=0,
        max_security_findings_high=0,
        max_secrets=0,
    )
    missing = scanner_graders(None, policy)
    assert missing
    assert all(result.blocking and result.passed is False for result in missing)

    scans = ScanResults(
        lint_errors=1,
        sec_findings_high=0,
        secrets_found=0,
        scanner_status={"gitleaks": "ok"},
    )
    by_name = {result.name: result for result in scanner_graders(scans, policy)}
    assert by_name["scanner available: gitleaks"].passed is True
    assert by_name["scanner threshold: lint errors"].passed is False


def test_sanitize_gen_filename(tmp_path):
    from agent_eval.review import _sanitize_gen_filename

    assert _sanitize_gen_filename("tests/test_new.py", tmp_path) == "tests/test_new.py"
    assert _sanitize_gen_filename("../evil.py", tmp_path) is None
    assert _sanitize_gen_filename("/abs/path.py", tmp_path) is None
    assert _sanitize_gen_filename("a;rm -rf.py", tmp_path) is None
    (tmp_path / "test_x.py").write_text("existing")
    assert _sanitize_gen_filename("test_x.py", tmp_path) == "agent_eval_gen_test_x.py"

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    assert _sanitize_gen_filename("linked/test_escape.py", tmp_path) is None


def test_collect_changes_counts_untracked_files(tmp_path):
    from agent_eval.review import collect_changes

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"],
                   cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# demo\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True,
                   capture_output=True)

    source = tmp_path / "src" / "new_feature.py"
    source.parent.mkdir()
    source.write_text("def hello():\n    return 'world'\n")

    files, stats, diff = collect_changes(tmp_path, "HEAD", None)
    by_path = {f.path: f for f in files}

    assert by_path["src/new_feature.py"].lines_added == 2
    assert stats.lines_added == 2
    assert "diff --git a/src/new_feature.py b/src/new_feature.py" in diff
    assert "+def hello():" in diff


def test_working_tree_symlink_never_leaks_target_content(tmp_path):
    from agent_eval.review import collect_changes, snapshot_changed_files

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    outside = tmp_path / "private.txt"
    outside.write_text("DO_NOT_EXFILTRATE\n")
    (repo / "leak").symlink_to(outside)

    files, _, diff = collect_changes(repo, "HEAD", None)
    snapshot = tmp_path / "snapshot"
    assert snapshot_changed_files(repo, files, None, snapshot) == 1
    assert "DO_NOT_EXFILTRATE" not in diff
    assert "DO_NOT_EXFILTRATE" not in (snapshot / "leak").read_text()
    assert "new file mode 120000" in diff


def test_changed_tracked_symlink_serializes_only_its_link_target(tmp_path):
    from agent_eval.review import collect_changes, snapshot_changed_files

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    link = repo / "config-link"
    link.symlink_to("old-target")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    outside = tmp_path / "private.txt"
    outside.write_text("DO_NOT_EXFILTRATE\n")
    link.unlink()
    link.symlink_to(outside)

    files, _, diff = collect_changes(repo, "HEAD", None)
    snapshot = tmp_path / "snapshot"
    assert snapshot_changed_files(repo, files, None, snapshot) == 1
    assert "DO_NOT_EXFILTRATE" not in diff
    assert (snapshot / "config-link").read_text() == os.fspath(outside)


def test_reverse_test_injection_refuses_symlinked_parent(tmp_path):
    from agent_eval.graders import reverse_test_grader

    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "linked").symlink_to(outside, target_is_directory=True)

    result = reverse_test_grader(
        "true", base, {"linked/test_escape.py": "def test_noop(): pass\n"}
    )

    assert result.passed is None
    assert "unsafe test injection refused" in result.details
    assert not (outside / "test_escape.py").exists()


def test_review_skips_external_model_when_secret_screening_fails(
    monkeypatch, tmp_path
):
    from agent_eval import review
    from agent_eval.evaluators import scanners
    from agent_eval.metrics import ScanResults

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    source = repo / "app.py"
    source.write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    source.write_text("token = 'detected-secret'\n")

    monkeypatch.setattr(
        scanners,
        "run_scanners",
        lambda *args, **kwargs: ScanResults(
            secrets_found=1,
            scanner_status={"gitleaks": "ok"},
        ),
    )
    monkeypatch.setattr(
        review,
        "run_llm_review",
        lambda *args, **kwargs: pytest.fail("unsafe diff reached external model"),
    )

    report = review.review_change(
        repo,
        base="HEAD",
        run_scans=True,
        run_llm=True,
        out_dir=tmp_path / "report",
    )

    assert report.llm is None
    assert report.blocked is True
    assert any(
        grader.name == "external model input has passed secret screening"
        and grader.passed is False
        for grader in report.graders
    )


def test_review_context_is_inside_secret_screening_boundary(
    monkeypatch, tmp_path
):
    from agent_eval import review
    from agent_eval.evaluators import scanners
    from agent_eval.metrics import ScanResults

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    source = repo / "app.py"
    source.write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    source.write_text("value = 2\n")
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"

    def fake_scanners(scan_root, *args, **kwargs):
        metadata = (scan_root / "agent-eval-review-metadata.txt").read_text()
        screened_diff = (scan_root / "agent-eval-change.diff").read_text()
        assert secret in metadata
        assert "diff --git" in screened_diff
        return ScanResults(
            secrets_found=1,
            scanner_status={"gitleaks": "ok"},
        )

    monkeypatch.setattr(scanners, "run_scanners", fake_scanners)
    monkeypatch.setattr(
        review,
        "run_llm_review",
        lambda *args, **kwargs: pytest.fail("secret context reached model"),
    )

    report = review.review_change(
        repo,
        base="HEAD",
        context=f"ticket token: {secret}",
        out_dir=tmp_path / "report",
    )

    assert report.llm is None
    assert report.blocked is True


def test_generated_test_prompt_uses_screened_snapshot_not_mutated_worktree(
    monkeypatch, tmp_path
):
    from agent_eval import review

    repo = tmp_path / "repo"
    snapshot = tmp_path / "screened"
    repo.mkdir()
    snapshot.mkdir()
    (repo / "app.py").write_text("token = 'MUTATED_AFTER_SCAN'\n")
    (snapshot / "app.py").write_text("value = 'screened-safe'\n")
    monkeypatch.setattr(review, "_git", lambda *args, **kwargs: "")

    _, user = review._build_gen_test_prompts(
        repo,
        None,
        "pytest -q",
        "+value = 'screened-safe'\n",
        [review.ChangedFile(path="app.py", status="M")],
        source_root=snapshot,
    )

    assert "screened-safe" in user
    assert "MUTATED_AFTER_SCAN" not in user


def test_generated_test_repair_never_sends_runtime_output_to_model(
    monkeypatch, tmp_path
):
    from contextlib import contextmanager

    from agent_eval import graders, review

    head = tmp_path / "head"
    base = tmp_path / "base"
    out = tmp_path / "out"
    head.mkdir()
    base.mkdir()
    out.mkdir()
    runtime_secret = "AWS_SECRET_ACCESS_KEY=do-not-send-this-value"
    model_users = []

    monkeypatch.setattr(
        review,
        "_build_gen_test_prompts",
        lambda *args, **kwargs: ("system", "screened source context"),
    )

    responses = [
        review.GeneratedTest(
            filename="tests/test_generated.py",
            code="def test_generated(): assert False\n",
        ),
        review.GeneratedTest(
            filename="tests/test_generated.py",
            code="def test_generated(): assert True\n",
        ),
    ]

    def fake_completion(system, user, *args, **kwargs):
        del system, args, kwargs
        model_users.append(user)
        return responses.pop(0), "test-model"

    exits = iter(
        [
            (1, runtime_secret),
            (0, "head pass"),
            (0, "baseline pass"),
            (1, "base correctly fails"),
        ]
    )
    monkeypatch.setattr(
        "agent_eval.evaluators.judge.structured_completion", fake_completion
    )
    monkeypatch.setattr(graders, "_run_shell", lambda *args, **kwargs: next(exits))

    @contextmanager
    def fake_worktree(repo, anchor):
        del repo, anchor
        yield base

    monkeypatch.setattr(review, "worktree", fake_worktree)

    review.run_generated_test_graders(
        head,
        None,
        "HEAD",
        "pytest -q",
        "+changed\n",
        [],
        head,
        out,
    )

    assert len(model_users) == 2
    assert runtime_secret not in model_users[1]
    assert "Runtime output is intentionally withheld" in model_users[1]


def test_collect_changes_preserves_renamed_head_paths(tmp_path):
    from agent_eval.review import collect_changes, snapshot_changed_files

    for index, destination in enumerate(("src/new.py", "lib/moved.py")):
        repo = tmp_path / f"repo-{index}"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                       cwd=repo, check=True)
        source = repo / "src" / "old.py"
        source.parent.mkdir()
        source.write_text("".join(f"value_{line} = {line}\n" for line in range(100)))
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo,
                       check=True, capture_output=True)

        target = repo / destination
        target.parent.mkdir(exist_ok=True)
        subprocess.run(["git", "mv", "src/old.py", destination], cwd=repo,
                       check=True)
        with target.open("a") as handle:
            handle.write("danger = eval(user_input)\n")

        files, stats, _ = collect_changes(repo, "HEAD", None)

        assert len(files) == 1
        assert files[0].path == destination
        assert files[0].status == "R"
        assert files[0].head_line_ranges == [(101, 101)]
        assert stats.lines_added == 1
        snapshot = repo / "snapshot"
        assert snapshot_changed_files(repo, files, None, snapshot) == 1
        assert (snapshot / destination).read_text().endswith(
            "danger = eval(user_input)\n"
        )
