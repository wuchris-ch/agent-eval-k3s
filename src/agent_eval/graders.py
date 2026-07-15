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
import os
import selectors
import signal
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterator

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .metrics import ScanResults
from .yaml_utils import load_unique_yaml

COMMAND_TIMEOUT = 600
TEST_TIMEOUT = 900
COMMAND_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024
COMMAND_OUTPUT_TAIL_BYTES = 4000
MAX_COMMAND_BYTES = 16 * 1024
_COMMAND_ENV_ALLOWLIST = (
    "CURL_CA_BUNDLE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "VIRTUAL_ENV",
)


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
    model_config = ConfigDict(extra="forbid")

    test_cmd: str | None = None
    checks: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)   # blocking
    allowed_paths: list[str] = Field(default_factory=list)   # blocking if set
    max_files: int | None = None                             # non-blocking
    max_lines: int | None = None                             # non-blocking
    require_tests_for: list[str] = Field(default_factory=list)  # non-blocking
    required_scanners: list[str] = Field(default_factory=list)
    max_lint_errors: int | None = Field(default=None, ge=0)
    max_security_findings_high: int | None = Field(default=None, ge=0)
    max_security_findings_medium: int | None = Field(default=None, ge=0)
    max_secrets: int | None = Field(default=None, ge=0)
    max_vulnerabilities: int | None = Field(default=None, ge=0)

    @staticmethod
    def _validate_command(value: str) -> str:
        if not value.strip():
            raise ValueError("review commands must not be empty")
        if "\0" in value:
            raise ValueError("review commands must not contain NUL bytes")
        if len(value.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise ValueError(
                f"review commands must not exceed {MAX_COMMAND_BYTES} UTF-8 bytes"
            )
        return value

    @field_validator("test_cmd")
    @classmethod
    def _valid_test_command(cls, value: str | None) -> str | None:
        return cls._validate_command(value) if value is not None else None

    @field_validator("checks")
    @classmethod
    def _valid_check_commands(cls, value: list[str]) -> list[str]:
        return [cls._validate_command(command) for command in value]

    @field_validator("required_scanners")
    @classmethod
    def _normalize_required_scanners(cls, value: list[str]) -> list[str]:
        result = []
        for scanner in value:
            normalized = scanner.strip().lower()
            if not normalized:
                raise ValueError("required scanner names must not be empty")
            if normalized not in result:
                result.append(normalized)
        return result


def load_policy(
    repo: Path,
    explicit: Path | None = None,
    *,
    trusted_ref: str | None = None,
) -> ReviewPolicy:
    def parse_policy(raw: str, source: str) -> ReviewPolicy:
        try:
            data = load_unique_yaml(raw) or {}
            if not isinstance(data, dict):
                raise ValueError("policy root must be a mapping")
            return ReviewPolicy(**(data.get("review") or data))
        except (yaml.YAMLError, ValueError) as exc:
            raise RuntimeError(f"invalid review policy {source}: {exc}") from exc

    if explicit is not None:
        path = explicit.expanduser()
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(
                f"explicit review policy is not a readable regular file: {path}"
            )
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"could not read explicit review policy {path}: {exc}") from exc
        return parse_policy(raw, str(path))

    if trusted_ref is not None and explicit is None:
        for name in (".agent-eval.yaml", ".agent-eval.yml"):
            proc = subprocess.run(
                ["git", "show", f"{trusted_ref}:{name}"],
                cwd=repo,
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                return parse_policy(proc.stdout, f"{trusted_ref}:{name}")
        return ReviewPolicy()
    candidates = [repo / ".agent-eval.yaml", repo / ".agent-eval.yml"]
    for path in candidates:
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeError(f"could not read review policy {path}: {exc}") from exc
            return parse_policy(raw, str(path))
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


def _command_environment(private_root: Path) -> dict[str, str]:
    environment = {
        name: value
        for name in _COMMAND_ENV_ALLOWLIST
        if (value := os.environ.get(name)) is not None
    }
    environment.setdefault("PATH", os.defpath)
    environment.update(
        {
            "HOME": str(private_root / "home"),
            "TMPDIR": str(private_root / "tmp"),
            "TMP": str(private_root / "tmp"),
            "TEMP": str(private_root / "tmp"),
            "XDG_CACHE_HOME": str(private_root / "cache"),
            "XDG_CONFIG_HOME": str(private_root / "config"),
            "XDG_DATA_HOME": str(private_root / "data"),
            "XDG_STATE_HOME": str(private_root / "state"),
        }
    )
    return environment


def _terminate_command(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    process.wait()


def _append_command_tail(tail: bytearray, chunk: bytes) -> None:
    tail.extend(chunk)
    overflow = len(tail) - COMMAND_OUTPUT_TAIL_BYTES
    if overflow > 0:
        del tail[:overflow]


def _run_shell(
    cmd: str,
    cwd: Path,
    timeout: float,
) -> tuple[int | None, str]:
    try:
        ReviewPolicy._validate_command(cmd)
    except (UnicodeEncodeError, ValueError) as exc:
        return None, f"invalid command: {exc}"

    with tempfile.TemporaryDirectory(prefix="agent-eval-command-") as temporary:
        private_root = Path(temporary)
        for name in ("home", "tmp", "cache", "config", "data", "state"):
            (private_root / name).mkdir(mode=0o700)
        try:
            process = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                cwd=cwd,
                env=_command_environment(private_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return None, f"could not start command: {type(exc).__name__}"

        assert process.stdout is not None
        output = process.stdout
        os.set_blocking(output.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(output, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        total = 0
        tail = bytearray()
        failure: str | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = f"timed out after {timeout:g}s"
                    break
                for key, _ in selector.select(timeout=min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    total += len(chunk)
                    _append_command_tail(tail, chunk)
                    if total > COMMAND_OUTPUT_LIMIT_BYTES:
                        failure = (
                            "output exceeded the "
                            f"{COMMAND_OUTPUT_LIMIT_BYTES}-byte limit"
                        )
                        break
                if failure is not None:
                    break
        finally:
            selector.close()
            if not output.closed:
                output.close()
            _terminate_command(process)

        detail = tail.decode("utf-8", errors="replace")
        if failure is not None:
            return None, failure
        return process.returncode, detail


def _safe_injection_target(workspace: Path, relative: str) -> Path:
    """Resolve a worktree-relative write target without following symlinks."""

    pure = PurePosixPath(relative)
    if (
        not relative
        or "\\" in relative
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        raise ValueError("injection path must be a canonical worktree-relative path")
    root = workspace.resolve(strict=True)
    current = root
    for index, part in enumerate(pure.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"injection path contains symlink: {relative}")
        if index < len(pure.parts) - 1 and not stat.S_ISDIR(mode):
            raise ValueError(f"injection parent is not a directory: {relative}")
        if index == len(pure.parts) - 1 and stat.S_ISDIR(mode):
            raise ValueError(f"injection target is a directory: {relative}")
    return root.joinpath(*pure.parts)


def _write_injected_file(
    workspace: Path, relative: str, content: str, *, overwrite: bool
) -> Path:
    target = _safe_injection_target(workspace, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _safe_injection_target(workspace, relative)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    fd = os.open(target, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return target


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
    baseline_code, baseline_tail = _run_shell(test_cmd, base_tree, TEST_TIMEOUT)
    if baseline_code is None:
        return GraderResult(
            name="new/changed tests vs base commit",
            category="reverse-classical",
            blocking=False,
            passed=None,
            details=(
                "base suite timed out before test injection; discrimination "
                "could not be established"
            ),
            output_tail=baseline_tail,
            duration_s=round(time.monotonic() - start, 1),
        )
    if baseline_code != 0:
        return GraderResult(
            name="new/changed tests vs base commit",
            category="reverse-classical",
            blocking=False,
            passed=None,
            details=(
                f"base suite already fails (exit {baseline_code}) before test "
                "injection; discrimination could not be established"
            ),
            output_tail=baseline_tail,
            duration_s=round(time.monotonic() - start, 1),
        )
    for rel, content in test_files.items():
        try:
            _write_injected_file(base_tree, rel, content, overwrite=True)
        except (OSError, ValueError) as exc:
            return GraderResult(
                name="new/changed tests vs base commit",
                category="reverse-classical",
                blocking=False,
                passed=None,
                details=f"unsafe test injection refused: {exc}",
                duration_s=round(time.monotonic() - start, 1),
            )
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


def scanner_graders(
    scans: ScanResults | None, policy: ReviewPolicy
) -> list[GraderResult]:
    """Create blocking, fail-closed graders for configured scan evidence."""

    results: list[GraderResult] = []
    statuses = scans.scanner_status if scans is not None else {}
    for scanner in policy.required_scanners:
        state = statuses.get(scanner)
        passed = state in {"ok", "not_applicable"}
        results.append(
            GraderResult(
                name=f"scanner available: {scanner}",
                category="scanner",
                blocking=True,
                passed=passed,
                details=(
                    f"status {state}"
                    if state is not None
                    else "scanner evidence unavailable"
                ),
            )
        )

    thresholds = (
        ("lint errors", scans.lint_errors if scans else None, policy.max_lint_errors),
        (
            "high security findings",
            scans.sec_findings_high if scans else None,
            policy.max_security_findings_high,
        ),
        (
            "medium security findings",
            scans.sec_findings_medium if scans else None,
            policy.max_security_findings_medium,
        ),
        ("secrets", scans.secrets_found if scans else None, policy.max_secrets),
        (
            "vulnerabilities",
            scans.vulns if scans else None,
            policy.max_vulnerabilities,
        ),
    )
    for name, observed, maximum in thresholds:
        if maximum is None:
            continue
        results.append(
            GraderResult(
                name=f"scanner threshold: {name}",
                category="scanner",
                blocking=True,
                passed=observed is not None and observed <= maximum,
                details=(
                    f"observed {observed}; maximum {maximum}"
                    if observed is not None
                    else f"evidence unavailable; maximum {maximum}"
                ),
            )
        )
    return results
