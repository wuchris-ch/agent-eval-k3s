"""Run pipeline: (agent phase) -> snapshot/diff -> eval phase -> scans -> judge.

Eval phase runs in a fresh pod so the coding agent cannot have poisoned the
test environment; hidden tests only ever exist in the eval pod."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from rich.console import Console

from .cluster import SECRET_NAME, build_and_import_image
from .evaluators.tests import TestResults, parse_coverage, parse_junit
from .kube import create_sandbox_pod, ensure_namespace
from .metrics import DiffStats, RunRecord, now_iso, save_run
from .task import Task

console = Console()


def new_run_id(task: Task, agent: str) -> str:
    return f"{task.id}--{agent}--{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def ensure_image(task: Task, rebuild: bool = False) -> None:
    if not rebuild:
        proc = subprocess.run(["docker", "image", "inspect", task.image_tag],
                              capture_output=True)
        if proc.returncode == 0:
            # image exists locally; still import in case the cluster was recreated
            subprocess.run(["k3d", "image", "import", task.image_tag, "-c", "agent-eval"],
                           capture_output=True)
            return
    build_and_import_image(str(task.environment_dir), task.image_tag)


def run_eval_phase(task: Task, workspace: Path, run_dir: Path) -> TestResults:
    """Copy a produced workspace + hidden tests into a fresh pod, run the task's
    test command, and pull back /results for parsing."""
    ensure_namespace()
    pod = create_sandbox_pod("eval", task.image_tag,
                             active_deadline=task.timeouts.eval_seconds + 900)
    try:
        pod.wait_ready()
        pod.copy_dir_to(workspace, "/workspace")
        pod.copy_dir_to(task.tests_dir, "/tests")
        pod.exec("mkdir -p /results", timeout=30)

        console.print("running hidden tests in eval pod...")
        try:
            proc = pod.exec(f"cd /workspace && {task.test_command}",
                            timeout=task.timeouts.eval_seconds)
            output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
        except subprocess.TimeoutExpired:
            (run_dir / "eval-output.txt").write_text("TIMEOUT")
            return TestResults(infra_error=f"test command timed out after "
                                           f"{task.timeouts.eval_seconds}s")
        (run_dir / "eval-output.txt").write_text(output)

        results_dir = run_dir / "results"
        pod.copy_dir_from("/results", results_dir)
        test_results = parse_junit(results_dir / "junit.xml")
        test_results.coverage_percent = parse_coverage(results_dir / "coverage.json")
        return test_results
    finally:
        pod.delete()


# derived artifacts the agent's tooling generates; excluded from diffing
JUNK_DIR_PATTERNS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".git",
                     "node_modules", ".venv", ".codex", ".claude", "*.pyc")


def compute_diff(starter: Path, produced: Path, run_dir: Path) -> DiffStats:
    """Diff the starter workspace against what the agent produced, ignoring
    derived artifacts (bytecode caches, agent config dirs, etc.)."""
    with tempfile.TemporaryDirectory(prefix="agent-eval-diff-") as tmp:
        ignore = shutil.ignore_patterns(*JUNK_DIR_PATTERNS)
        shutil.copytree(starter, Path(tmp) / "a", ignore=ignore)
        shutil.copytree(produced, Path(tmp) / "b", ignore=ignore)

        def git_diff(*flags: str) -> str:
            proc = subprocess.run(
                ["git", "-c", "core.quotePath=false", "diff", "--no-index",
                 *flags, "a", "b"],
                capture_output=True, text=True, cwd=tmp,
            )
            return proc.stdout

        (run_dir / "workspace.diff").write_text(git_diff())
        stats = DiffStats()
        for line in git_diff("--numstat").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                stats.files_changed += 1
                if parts[0] != "-":  # binary files report "-"
                    stats.lines_added += int(parts[0])
                    stats.lines_removed += int(parts[1])
        return stats


def evaluate_workspace(task: Task, workspace: Path, *, agent: str = "external",
                       trial: int = 1, run_id: str | None = None,
                       record: RunRecord | None = None,
                       run_scans: bool = True, run_judge: bool = True) -> RunRecord:
    """Eval pipeline on an already-produced workspace: tests, diff, scans, judge."""
    if record is None:
        record = RunRecord(run_id=run_id or new_run_id(task, agent),
                           task_id=task.id, agent=agent, trial=trial,
                           started_at=now_iso())
    run_dir = record.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    ensure_image(task)
    record.correctness = run_eval_phase(task, workspace, run_dir)
    record.diff = compute_diff(task.workspace_dir, workspace, run_dir)

    if run_scans:
        from .evaluators.scanners import run_scanners
        record.scans = run_scanners(task, workspace, run_dir)
    if run_judge and task.judge.enabled:
        from .evaluators.judge import run_judge as judge_workspace
        record.judge = judge_workspace(task, run_dir)

    record.finished_at = now_iso()
    save_run(record)
    return record


def run_agent_trial(task: Task, adapter, *, trial: int = 1, model: str | None = None,
                    run_scans: bool = True, run_judge: bool = True) -> RunRecord:
    """Full-harness trial: launch the coding agent in a sandbox pod, snapshot
    its workspace, then evaluate that workspace."""
    record = RunRecord(run_id=new_run_id(task, adapter.name), task_id=task.id,
                       agent=adapter.name, trial=trial, started_at=now_iso())
    run_dir = record.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    ensure_namespace()
    pod = create_sandbox_pod("agent", task.image_tag, env_from_secret=SECRET_NAME,
                             active_deadline=task.timeouts.agent_seconds + 900)
    produced = run_dir / "workspace"
    try:
        pod.wait_ready()
        with tempfile.TemporaryDirectory() as tmp:
            from .agents import PROMPT_PATH
            prompt_dir = Path(tmp)
            (prompt_dir / Path(PROMPT_PATH).name).write_text(task.prompt)
            pod.copy_dir_to(prompt_dir, str(Path(PROMPT_PATH).parent))
        if hasattr(adapter, "prepare"):
            adapter.prepare(pod)

        console.print(f"running [bold]{adapter.name}[/bold] in sandbox pod "
                      f"(timeout {task.timeouts.agent_seconds}s)...")
        start = time.monotonic()
        timed_out = False
        try:
            proc = pod.exec(adapter.build_command(model),
                            timeout=task.timeouts.agent_seconds, env=adapter.env)
            (run_dir / "transcript.jsonl").write_bytes(proc.stdout)
            (run_dir / "agent-stderr.log").write_bytes(proc.stderr)
            record.efficiency = adapter.parse_transcript(run_dir / "transcript.jsonl")
            record.efficiency.agent_exit_code = proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            (run_dir / "transcript.jsonl").write_bytes(e.stdout or b"")
            (run_dir / "agent-stderr.log").write_bytes(
                (e.stderr or b"") + b"\nAGENT TIMED OUT")
            record.efficiency = adapter.parse_transcript(run_dir / "transcript.jsonl")
        record.efficiency.wall_time_s = round(time.monotonic() - start, 1)
        if timed_out:
            console.print("[yellow]agent timed out; evaluating partial work[/yellow]")

        # snapshot whatever the agent produced (the sleep pod is still alive)
        pod.copy_dir_from("/workspace", produced)
    finally:
        pod.delete()

    return evaluate_workspace(task, produced, record=record,
                              run_scans=run_scans, run_judge=run_judge)


def validate_task(task: Task) -> RunRecord:
    """Overlay the oracle solution onto the starter workspace and require the
    hidden tests to pass. Proves the task + eval path work without any LLM."""
    problems = task.validate_layout()
    if problems:
        raise ValueError(f"task {task.id} layout invalid: {', '.join(problems)}")
    if not task.solution_dir.is_dir():
        raise ValueError(f"task {task.id} has no solution/ directory to validate with")

    with tempfile.TemporaryDirectory(prefix="agent-eval-oracle-") as tmp:
        oracle_ws = Path(tmp) / "workspace"
        shutil.copytree(task.workspace_dir, oracle_ws)
        shutil.copytree(task.solution_dir, oracle_ws, dirs_exist_ok=True)
        return evaluate_workspace(task, oracle_ws, agent="oracle",
                                  run_scans=False, run_judge=False)
