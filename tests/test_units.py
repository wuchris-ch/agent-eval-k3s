import json
import subprocess
from pathlib import Path

from agent_eval.agents.claude_code import ClaudeCodeAdapter
from agent_eval.evaluators.tests import parse_coverage, parse_junit
from agent_eval.report import pass_at_k
from agent_eval.task import load_task

REPO = Path(__file__).resolve().parents[1]


def test_load_example_task():
    task = load_task("example-todo-api")
    assert task.image_tag == "agent-eval/example-todo-api:latest"
    assert "junit.xml" in task.test_command
    assert task.validate_layout() == []
    assert abs(sum(task.judge.weights.values()) - 1.0) < 1e-9


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


def test_parse_coverage(tmp_path):
    cov = tmp_path / "coverage.json"
    cov.write_text(json.dumps({"totals": {"percent_covered": 87.5}}))
    assert parse_coverage(cov) == 87.5
    assert parse_coverage(tmp_path / "nope.json") is None


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


def test_verify_findings():
    from agent_eval.review import Finding, verify_findings

    diff = ("--- a/src/auth.py\n+++ b/src/auth.py\n"
            "+    return hashlib.md5(password.encode()).hexdigest()\n")
    changed = ["src/auth.py"]

    good = Finding(severity="major", file="src/auth.py",
                   claim="weak hash",
                   evidence="hashlib.md5(password.encode()).hexdigest()")
    paraphrase = Finding(severity="major", file="src/auth.py",
                         claim="weak hash", evidence="uses an md5 digest call")
    wrong_file = Finding(severity="major", file="src/other.py",
                         claim="weak hash",
                         evidence="hashlib.md5(password.encode()).hexdigest()")
    too_short = Finding(severity="nit", file="src/auth.py",
                        claim="x", evidence="return")

    verified = verify_findings([good, paraphrase, wrong_file, too_short],
                               diff, changed)
    assert verified[0].verified is True
    assert verified[1].verified is False
    assert verified[2].verified is False
    assert verified[3].verified is False


def test_verify_findings_nit_cap():
    from agent_eval.review import MAX_NITS, Finding, verify_findings

    diff = "+    some perfectly quotable changed line of code here\n"
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
    assert llm_findings_risk(LLMReview(findings=[confirmed_major])) == "medium"


def test_compute_risk_with_graders():
    from agent_eval.graders import GraderResult
    from agent_eval.metrics import DiffStats
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


def test_sanitize_gen_filename(tmp_path):
    from agent_eval.review import _sanitize_gen_filename

    assert _sanitize_gen_filename("tests/test_new.py", tmp_path) == "tests/test_new.py"
    assert _sanitize_gen_filename("../evil.py", tmp_path) is None
    assert _sanitize_gen_filename("/abs/path.py", tmp_path) is None
    assert _sanitize_gen_filename("a;rm -rf.py", tmp_path) is None
    (tmp_path / "test_x.py").write_text("existing")
    assert _sanitize_gen_filename("test_x.py", tmp_path) == "agent_eval_gen_test_x.py"


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
