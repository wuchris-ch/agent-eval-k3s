"""Executable graders for change review, modeled on frontier code evals
(Cognition FrontierCode): every verdict that can be grounded in execution is,
and only what cannot be executed is left to the LLM.

Categories:
- command:           run a shell command in the head workspace; exit 0 passes
- classical (head):  the repo's test command must pass on the head side
- reverse-classical: new/changed tests are replayed against the base commit
                     and must FAIL there, proving they test the new behavior
- scope:             file boundaries and diff-size constraints from policy

Blocking graders gate the change (a failure forces overall risk to high);
non-blocking failures add weighted risk signals.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import yaml
from pydantic import BaseModel, Field

COMMAND_TIMEOUT = 600
TEST_TIMEOUT = 900


class GraderResult(BaseModel):
    name: str
    category: str  # command | classical | reverse-classical | scope | prompt
    passed: bool | None = None  # None = skipped / not applicable
    blocking: bool = True
    weight: int = 1  # risk-score contribution when a non-blocking grader fails
    details: str = ""
    output_tail: str = ""
    duration_s: float = 0.0


class ReviewPolicy(BaseModel):
    """Per-repo review policy, read from <repo>/.agent-eval.yaml.

    Path patterns are fnmatch globs matched against the full repo-relative
    path (`*` crosses directory separators, so `src/*` matches `src/a/b.py`).
    """
    test_cmd: str | None = None
    checks: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)   # blocking
    allowed_paths: list[str] = Field(default_factory=list)   # blocking if set
    max_files: int | None = None                             # non-blocking
    max_lines: int | None = None                             # non-blocking
    require_tests_for: list[str] = Field(default_factory=list)  # non-blocking


def load_policy(repo: Path, explicit: Path | None = None) -> ReviewPolicy:
    candidates = [explicit] if explicit else \
        [repo / ".agent-eval.yaml", repo / ".agent-eval.yml"]
    for path in candidates:
        if path and path.is_file():
            data = yaml.safe_load(path.read_text()) or {}
            return ReviewPolicy(**(data.get("review") or data))
    return ReviewPolicy()


# ---------------------------------------------------------------- git helpers

def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, cwd=repo)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:500]}")
    return proc.stdout


@contextmanager
def worktree(repo: Path, ref: str) -> Iterator[Path]:
    """Detached checkout of `ref` in a temp dir, removed on exit."""
    tmp = Path(tempfile.mkdtemp(prefix="agent-eval-wt-"))
    try:
        _git(repo, "worktree", "add", "--detach", str(tmp), ref)
        yield tmp
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(tmp)],
                       cwd=repo, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def head_workspace(repo: Path, head: str | None) -> Iterator[Path]:
    """The head side of the change as a directory: the repo itself when
    reviewing the working tree, otherwise a clean worktree of the head ref
    (so graders never run against a stale working tree)."""
    if head is None:
        yield repo
    else:
        with worktree(repo, head) as tree:
            yield tree


def _run_shell(cmd: str, cwd: Path, timeout: int) -> tuple[int | None, str]:
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, text=True,
                              capture_output=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"


# -------------------------------------------------------------------- graders

def command_grader(cmd: str, workspace: Path,
                   timeout: int = COMMAND_TIMEOUT) -> GraderResult:
    start = time.monotonic()
    code, tail = _run_shell(cmd, workspace, timeout)
    return GraderResult(
        name=f"check: {cmd}", category="command", blocking=True,
        passed=code == 0, output_tail=tail,
        details=f"exit {code}" if code is not None else tail,
        duration_s=round(time.monotonic() - start, 1))


def head_test_grader(test_cmd: str, workspace: Path) -> GraderResult:
    start = time.monotonic()
    code, tail = _run_shell(test_cmd, workspace, TEST_TIMEOUT)
    return GraderResult(
        name=f"tests on head: {test_cmd}", category="classical", blocking=True,
        passed=code == 0, output_tail=tail,
        details=f"exit {code}" if code is not None else tail,
        duration_s=round(time.monotonic() - start, 1))


def reverse_test_grader(test_cmd: str, base_tree: Path,
                        test_files: dict[str, str]) -> GraderResult:
    """Inject the head-side versions of new/changed test files into the base
    checkout and run the suite there. The suite must FAIL: tests that also
    pass on the base commit do not verify the new behavior."""
    start = time.monotonic()
    if not test_files:
        return GraderResult(name="new/changed tests vs base commit",
                            category="reverse-classical", blocking=False,
                            details="no new or changed tests to validate")
    for rel, content in test_files.items():
        target = base_tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    code, tail = _run_shell(test_cmd, base_tree, TEST_TIMEOUT)
    if code is None:
        passed, details = None, tail
    elif code != 0:
        passed = True
        details = (f"{len(test_files)} new/changed test file(s) fail against "
                   "the base commit, as they should")
    else:
        passed = False
        details = (f"{len(test_files)} new/changed test file(s) also PASS "
                   "against the base commit; they do not verify the new behavior")
    return GraderResult(name="new/changed tests vs base commit",
                        category="reverse-classical", blocking=False, weight=2,
                        passed=passed, details=details, output_tail=tail,
                        duration_s=round(time.monotonic() - start, 1))


def _match_any(path: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if fnmatch.fnmatch(path, pat):
            return pat
    return None


def scope_graders(paths_by_subsystem: list[tuple[str, str]],
                  total_lines: int, policy: ReviewPolicy) -> list[GraderResult]:
    """Deterministic file-boundary and size constraints from policy.
    `paths_by_subsystem` is (path, subsystem) for every changed file."""
    results: list[GraderResult] = []
    paths = [p for p, _ in paths_by_subsystem]

    if policy.blocked_paths:
        hits = [(p, _match_any(p, policy.blocked_paths)) for p in paths]
        hits = [(p, m) for p, m in hits if m]
        results.append(GraderResult(
            name="scope: blocked paths", category="scope", blocking=True,
            passed=not hits,
            details="; ".join(f"{p} matches blocked pattern {m}" for p, m in hits[:5])
                    or f"no changed file matches {policy.blocked_paths}"))

    if policy.allowed_paths:
        strays = [p for p in paths if not _match_any(p, policy.allowed_paths)]
        results.append(GraderResult(
            name="scope: allowed paths", category="scope", blocking=True,
            passed=not strays,
            details="; ".join(f"{p} outside allowed paths" for p in strays[:5])
                    or f"all changes within {policy.allowed_paths}"))

    if policy.max_files is not None:
        results.append(GraderResult(
            name=f"scope: at most {policy.max_files} files", category="scope",
            blocking=False, passed=len(paths) <= policy.max_files,
            details=f"{len(paths)} file(s) changed"))
    if policy.max_lines is not None:
        results.append(GraderResult(
            name=f"scope: at most {policy.max_lines} changed lines",
            category="scope", blocking=False,
            passed=total_lines <= policy.max_lines,
            details=f"{total_lines} line(s) changed"))

    if policy.require_tests_for:
        needing = [p for p, sub in paths_by_subsystem
                   if sub not in ("tests", "docs")
                   and _match_any(p, policy.require_tests_for)]
        has_tests = any(sub == "tests" for _, sub in paths_by_subsystem)
        if needing:
            results.append(GraderResult(
                name="scope: tests required for covered paths", category="scope",
                blocking=False, passed=has_tests,
                details=(f"{len(needing)} changed file(s) under "
                         f"{policy.require_tests_for} "
                         + ("with" if has_tests else "WITHOUT") + " test changes")))
    return results
