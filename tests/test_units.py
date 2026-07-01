import json
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
