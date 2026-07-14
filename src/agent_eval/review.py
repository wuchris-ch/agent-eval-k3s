"""Change review: pre-merge risk assessment of a git diff.

Works in any git repository with no cluster, image, or task definition:
    agent-eval review                      # working tree vs auto-detected base
    agent-eval review --base main --head feature-branch
    agent-eval review --test-cmd "pytest -q" --check "ruff check ."

Grader pipeline, modeled on frontier code evals (Cognition FrontierCode):
diff intelligence -> scope graders (policy file boundaries, size) -> scanners
over the changed files -> command graders (exit 0) -> classical grader (tests
pass on head) -> reverse-classical grader (new/changed tests must FAIL on the
base commit) -> optional LLM-generated discriminating test (classical +
adaptive repair) -> LLM findings review where every claim must carry a
normalized quote from an added or deleted diff run, is verified
programmatically, and blocker/major findings
must survive a second adversarial verification pass.

Deterministic evidence sets a risk floor; the LLM can escalate it only through
findings that survived verification, never through unsupported opinion.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

from .graders import (GraderResult, _git, _safe_injection_target,
                      _write_injected_file, command_grader, head_test_grader,
                      head_workspace, load_policy, reverse_test_grader,
                      scanner_graders, scope_graders, worktree)
from .metrics import DiffStats, ScanResults, now_iso

console = Console()
MAX_DIFF_CHARS = 60_000
MAX_GEN_CONTEXT_FILES = 5
MAX_GEN_FILE_CHARS = 6_000
MAX_NITS = 5
TEST_TIMEOUT = 900

RISK_LEVELS = ("low", "medium", "high")
SEVERITIES = ("blocker", "major", "minor", "nit")


class ChangedFile(BaseModel):
    path: str
    status: str = "M"  # A/M/D/R... or "?" for untracked
    lines_added: int = 0
    lines_removed: int = 0
    subsystem: str = "app code"
    head_line_ranges: list[tuple[int, int]] = Field(default_factory=list)


class TestRun(BaseModel):
    command: str
    exit_code: int | None = None
    passed: bool | None = None
    output_tail: str = ""


class Finding(BaseModel):
    severity: str = "minor"        # blocker | major | minor | nit
    category: str = "correctness"  # correctness|security|performance|tests|style
    file: str = ""
    line: int | None = None
    claim: str = ""
    evidence: str = ""             # normalized quote from one side of the diff
    evidence_side: str = "head"    # head (added) or base (deleted)
    evidence_line: int | None = None  # evidence-side line before head anchoring
    verified: bool = False         # quote located in the named file and side
    verdict: str | None = None     # confirmed | rejected (adversarial LLM pass)
    verdict_reason: str = ""

    @property
    def active(self) -> bool:
        if not self.verified or self.verdict == "rejected":
            return False
        if self.severity in ("blocker", "major"):
            return self.verdict == "confirmed"
        return True


class LLMReview(BaseModel):
    risk: str = "low"              # the model's holistic rating (recorded only;
    #                                risk escalation uses confirmed findings)
    summary: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    reviewer_focus: list[str] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)
    dropped_unverified: int = 0    # findings whose diff quote did not check out


class GeneratedTest(BaseModel):
    filename: str
    code: str
    notes: str = ""


class ChangeReport(BaseModel):
    repo: str
    base: str
    head: str  # ref name or "working tree"
    created_at: str = ""
    files: list[ChangedFile] = Field(default_factory=list)
    diff: DiffStats = Field(default_factory=DiffStats)
    signals: list[str] = Field(default_factory=list)
    heuristic_risk: str = "low"
    scans: ScanResults | None = None
    graders: list[GraderResult] = Field(default_factory=list)
    llm: LLMReview | None = None
    llm_model: str | None = None
    risk: str = "low"
    blocked: bool = False  # a blocking grader failed; exit 2 regardless of LLM
    report_dir: str = ""


# strict JSON schemas for codex --output-schema (additionalProperties: false)

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": list(SEVERITIES)},
        "category": {"type": "string",
                     "enum": ["correctness", "security", "performance",
                              "tests", "style"]},
        "file": {"type": "string"},
        "line": {"type": ["integer", "null"]},
        "claim": {"type": "string"},
        "evidence": {"type": "string"},
        "evidence_side": {"type": "string", "enum": ["head", "base"]},
    },
    "required": [
        "severity", "category", "file", "line", "claim", "evidence",
        "evidence_side",
    ],
    "additionalProperties": False,
}

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "risk": {"type": "string", "enum": list(RISK_LEVELS)},
        "summary": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": _FINDING_SCHEMA},
        "reviewer_focus": {"type": "array", "items": {"type": "string"}},
        "missing_tests": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["risk", "summary", "findings", "reviewer_focus", "missing_tests"],
    "additionalProperties": False,
}


class _FindingVerdict(BaseModel):
    index: int
    verdict: str = "rejected"  # confirmed | rejected
    reason: str = ""


class _VerdictResponse(BaseModel):
    verdicts: list[_FindingVerdict] = Field(default_factory=list)


_VERDICTS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "verdict": {"type": "string",
                                "enum": ["confirmed", "rejected"]},
                    "reason": {"type": "string"},
                },
                "required": ["index", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_GEN_TEST_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string"},
        "code": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["filename", "code", "notes"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- git plumbing

def resolve_base(repo: Path, base: str | None) -> str:
    if base:
        return base
    for cand in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        proc = subprocess.run(["git", "rev-parse", "--verify", "--quiet", cand],
                              capture_output=True, text=True, cwd=repo)
        if proc.returncode == 0:
            return cand
    raise RuntimeError("could not auto-detect a base branch; pass --base")


def _merge_base(repo: Path, base: str, head: str) -> str:
    proc = subprocess.run(["git", "merge-base", base, head],
                          capture_output=True, text=True, cwd=repo)
    return proc.stdout.strip() if proc.returncode == 0 else base


def _worktree_entry(repo: Path, path: str) -> tuple[Path, int] | None:
    pure = PurePosixPath(path)
    if (
        not path
        or "\\" in path
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in pure.parts)
    ):
        return None
    current = repo.resolve(strict=True)
    for index, part in enumerate(pure.parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None
        if index < len(pure.parts) - 1 and (
            stat.S_ISLNK(mode) or not stat.S_ISDIR(mode)
        ):
            return None
    return current, mode


def _head_file_content(repo: Path, head: str | None, path: str) -> str | None:
    try:
        if head is None:
            entry = _worktree_entry(repo, path)
            if entry is None:
                return None
            src, mode = entry
            if stat.S_ISLNK(mode):
                return os.readlink(src)
            return src.read_text() if stat.S_ISREG(mode) else None
        return _git(repo, "show", f"{head}:{path}")
    except (RuntimeError, OSError, UnicodeDecodeError):
        return None  # binary or vanished


# ---------------------------------------------------- subsystem classification

_SUBSYSTEM_RULES: list[tuple[str, tuple[str, ...]]] = [
    # (subsystem, lowercase substrings matched against path segments/names)
    ("tests", ("test_", "_test.", ".test.", ".spec.", "tests", "test", "spec",
               "conftest.py")),
    ("dependencies", ("pyproject.toml", "requirements", "package.json",
                      "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                      "uv.lock", "poetry.lock", "go.mod", "go.sum",
                      "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock")),
    ("auth/security", ("auth", "oauth", "login", "password", "passwd", "token",
                       "session", "permission", "acl", "crypto", "secret",
                       "jwt", "sso", "rbac")),
    ("data/migrations", ("migration", "migrations", "alembic", "schema",
                         "models", "database", "db")),
    ("ci/infra", (".github", ".gitlab-ci", "jenkinsfile", "dockerfile",
                  "docker-compose", "helm", "terraform", "k8s", "kubernetes",
                  "manifests", "deploy", "ansible", ".circleci")),
    ("docs", ("docs", "readme", "changelog", "license")),
]
_SUBSYSTEM_SUFFIXES = {
    ".sql": "data/migrations", ".tf": "ci/infra",
    ".md": "docs", ".rst": "docs",
}


def classify_subsystem(path: str) -> str:
    lower = path.lower()
    name = Path(lower).name
    parts = set(Path(lower).parts)
    for suffix, subsystem in _SUBSYSTEM_SUFFIXES.items():
        if lower.endswith(suffix):
            return subsystem
    for subsystem, needles in _SUBSYSTEM_RULES:
        for needle in needles:
            if needle in parts or needle in name:
                return subsystem
    return "app code"


# ------------------------------------------------------------- diff collection

# our own reports plus derived artifacts must never be part of the review
_EXCLUDE_PATHSPECS = ("--", ".", ":(exclude).agent-eval",
                      ":(exclude)**/__pycache__/**", ":(exclude)**/*.pyc",
                      ":(exclude)**/node_modules/**", ":(exclude)**/.venv/**")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<base>\d+)(?:,\d+)? \+(?P<head>\d+)(?:,\d+)? @@"
)


def _patch_path(value: str, changed_paths: set[str]) -> str | None:
    raw = value.strip()
    if raw == "/dev/null":
        return None
    if raw.startswith('"'):
        try:
            raw = ast.literal_eval(raw)
        except (SyntaxError, ValueError):
            return None
    if raw.startswith(("a/", "b/")):
        raw = raw[2:]
    if raw in changed_paths:
        return raw
    matches = [path for path in changed_paths if path.endswith(f"/{raw}")]
    return matches[0] if len(matches) == 1 else None


def _parse_head_line_ranges(
    diff_text: str, changed_paths: list[str]
) -> dict[str, list[tuple[int, int]]]:
    known_paths = set(changed_paths)
    ranges: dict[str, list[list[int]]] = {path: [] for path in changed_paths}
    current_path: str | None = None
    head_line: int | None = None

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current_path = None
            head_line = None
            continue
        if head_line is None and line.startswith("+++ "):
            current_path = _patch_path(line[4:], known_paths)
            head_line = None
            continue

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            head_line = int(hunk.group("head")) if current_path else None
            continue
        if current_path is None or head_line is None:
            continue
        if line.startswith("+"):
            path_ranges = ranges[current_path]
            if path_ranges and path_ranges[-1][1] + 1 == head_line:
                path_ranges[-1][1] = head_line
            else:
                path_ranges.append([head_line, head_line])
            head_line += 1
        elif line.startswith("-") or line.startswith("\\ No newline"):
            continue
        else:
            head_line += 1

    return {
        path: [(start, end) for start, end in path_ranges]
        for path, path_ranges in ranges.items()
    }


def _parse_name_status(output: str) -> dict[str, str]:
    fields = output.split("\0")
    status: dict[str, str] = {}
    index = 0
    while index < len(fields):
        marker = fields[index]
        index += 1
        if not marker or index >= len(fields):
            continue
        path = fields[index]
        index += 1
        if marker[:1] in ("R", "C"):
            if index >= len(fields):
                break
            path = fields[index]
            index += 1
        status[path] = marker[:1]
    return status


def _parse_numstat(output: str) -> list[tuple[int, int, str]]:
    fields = output.split("\0")
    parsed: list[tuple[int, int, str]] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        parts = entry.split("\t", 2)
        if len(parts) != 3:
            continue
        path = parts[2]
        if not path:
            if index + 1 >= len(fields):
                break
            path = fields[index + 1]
            index += 2
        added = int(parts[0]) if parts[0] != "-" else 0
        removed = int(parts[1]) if parts[1] != "-" else 0
        parsed.append((added, removed, path))
    return parsed


def collect_changes(repo: Path, base: str, head: str | None,
                    ) -> tuple[list[ChangedFile], DiffStats, str]:
    """Changed files + stats + unified diff for base...head (or base...worktree)."""
    anchor = _merge_base(repo, base, head or "HEAD")
    diff_args = [anchor, head, *_EXCLUDE_PATHSPECS] if head \
        else [anchor, *_EXCLUDE_PATHSPECS]
    diff_text = _git(repo, "diff", "-M", *diff_args)

    status = _parse_name_status(
        _git(repo, "diff", "--name-status", "-z", "-M", *diff_args)
    )

    files: list[ChangedFile] = []
    stats = DiffStats()
    for added, removed, path in _parse_numstat(
        _git(repo, "diff", "--numstat", "-z", "-M", *diff_args)
    ):
        files.append(ChangedFile(path=path, status=status.get(path, "M"),
                                 lines_added=added, lines_removed=removed,
                                 subsystem=classify_subsystem(path)))
        stats.files_changed += 1
        stats.lines_added += added
        stats.lines_removed += removed

    if head is None:  # untracked files never show in git diff
        junk = ("*.pyc", "*__pycache__*", ".agent-eval/*", "*node_modules*",
                "*.venv/*")
        untracked = _git(
            repo, "ls-files", "-z", "--others", "--exclude-standard"
        ).split("\0")
        for path in untracked:
            if path and not any(fnmatch.fnmatch(path, p) for p in junk):
                entry = _worktree_entry(repo, path)
                content = _head_file_content(repo, None, path) or ""
                is_symlink = entry is not None and stat.S_ISLNK(entry[1])
                added = len(content.splitlines()) if content else 0
                files.append(ChangedFile(path=path, status="?",
                                         lines_added=added,
                                         subsystem=classify_subsystem(path)))
                stats.files_changed += 1
                stats.lines_added += added
                if content:
                    lines = content.splitlines(keepends=True)
                    body = "".join(difflib.unified_diff(
                        [], lines, fromfile="/dev/null", tofile=f"b/{path}"))
                    mode = "120000" if is_symlink else "100644"
                    diff_text += (
                        f"\ndiff --git a/{path} b/{path}\n"
                        f"new file mode {mode}\n{body}"
                    )

    head_line_ranges = _parse_head_line_ranges(
        diff_text, [changed.path for changed in files]
    )
    for changed in files:
        changed.head_line_ranges = head_line_ranges.get(changed.path, [])

    return files, stats, diff_text


def snapshot_changed_files(repo: Path, files: list[ChangedFile],
                           head: str | None, dest: Path) -> int:
    """Copy the head-side version of every changed (non-deleted) file into dest
    so scanners run on just the change surface, not the whole repo."""
    copied = 0
    for f in files:
        if f.status == "D":
            continue
        content = _head_file_content(repo, head, f.path)
        if content is None:
            continue
        pure = PurePosixPath(f.path)
        if (
            "\\" in f.path
            or pure.is_absolute()
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            continue
        target = dest.joinpath(*pure.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        copied += 1
    return copied


def _scope_scans_to_changed_lines(
    scans: ScanResults, files: list[ChangedFile]
) -> None:
    ranges = {changed.path: changed.head_line_ranges for changed in files}
    scoped_findings: list[dict] = []
    for finding in scans.findings:
        if not isinstance(finding, dict):
            continue
        raw_path = str(finding.get("path") or "").replace("\\", "/").strip("/")
        if raw_path in ranges:
            path = raw_path
        else:
            matches = {
                changed_path
                for changed_path in ranges
                if raw_path.endswith(f"/{changed_path}")
                or changed_path.endswith(f"/{raw_path}")
            }
            if len(matches) != 1:
                continue
            path = next(iter(matches))
        line = finding.get("line")
        if (
            isinstance(line, bool)
            or not isinstance(line, int)
            or not any(start <= line <= end for start, end in ranges[path])
        ):
            continue
        finding["path"] = path
        scoped_findings.append(finding)

    scans.findings = scoped_findings
    semgrep_severities = [
        str(finding.get("severity") or "").upper()
        for finding in scoped_findings
        if str(finding.get("tool") or "").casefold() == "semgrep"
    ]
    if scans.sec_findings_high is not None:
        scans.sec_findings_high = semgrep_severities.count("ERROR")
    if scans.sec_findings_medium is not None:
        scans.sec_findings_medium = semgrep_severities.count("WARNING")
    if scans.sec_findings_low is not None:
        scans.sec_findings_low = semgrep_severities.count("INFO")
    if scans.secrets_found is not None:
        scans.secrets_found = sum(
            str(finding.get("tool") or "").casefold() == "gitleaks"
            for finding in scoped_findings
        )


# ---------------------------------------------------------------- risk signals

def compute_risk(files: list[ChangedFile], stats: DiffStats,
                 scans: ScanResults | None, tests: TestRun | None,
                 graders: list[GraderResult] | None = None,
                 ) -> tuple[list[str], str]:
    """Deterministic, repeatable risk signals. Returns (signals, low|medium|high)."""
    signals: list[str] = []
    score = 0
    subsystems = {f.subsystem for f in files}

    if "auth/security" in subsystems:
        signals.append("touches auth/security code")
        score += 2
    if "data/migrations" in subsystems:
        signals.append("touches data/schema/migration code")
        score += 2
    if "dependencies" in subsystems:
        signals.append("changes dependencies")
        score += 1
    if "ci/infra" in subsystems:
        signals.append("changes CI/infra configuration")
        score += 1
    total_lines = stats.lines_added + stats.lines_removed
    if total_lines > 500:
        signals.append(f"large diff ({total_lines} lines)")
        score += 1
    if stats.files_changed > 20:
        signals.append(f"wide diff ({stats.files_changed} files)")
        score += 1

    code_changed = any(f.subsystem not in ("tests", "docs") for f in files)
    tests_changed = any(f.subsystem == "tests" for f in files)
    if code_changed and not tests_changed:
        signals.append("code changed with no test changes")
        score += 1
    removed_tests = [f for f in files if f.subsystem == "tests"
                     and (f.status == "D" or f.lines_removed > f.lines_added)]
    if removed_tests:
        signals.append(f"tests removed or net-deleted ({len(removed_tests)} file(s))")
        score += 1

    force_high = False
    if scans:
        if scans.secrets_found:
            signals.append(f"secrets detected ({scans.secrets_found})")
            force_high = True
        if scans.sec_findings_high:
            signals.append(f"high-severity security findings ({scans.sec_findings_high})")
            force_high = True
    if tests and tests.passed is False:
        signals.append("test command failed")
        force_high = True

    for g in graders or []:
        if g.passed is False:
            if g.blocking:
                signals.append(f"blocking grader failed: {g.name}")
                force_high = True
            else:
                signals.append(f"grader failed: {g.name} ({g.details})")
                score += g.weight

    risk = "high" if force_high or score >= 4 else "medium" if score >= 2 else "low"
    return signals, risk


def _max_risk(*levels: str) -> str:
    known = [level for level in levels if level in RISK_LEVELS]
    return max(known, key=RISK_LEVELS.index) if known else "low"


# ------------------------------------------------------------ generated tests

_SAFE_FILENAME = re.compile(r"^[\w./-]+$")


def _sanitize_gen_filename(name: str, workspace: Path) -> str | None:
    name = name.strip()
    if not name or name.startswith(("/", "\\")) or ".." in name.split("/"):
        return None
    name = name.lstrip("./")
    if not name or not _SAFE_FILENAME.match(name):
        return None
    try:
        target = _safe_injection_target(workspace, name)
    except (OSError, ValueError):
        return None
    if target.exists():  # never overwrite a real file
        parts = name.rsplit("/", 1)
        parts[-1] = "agent_eval_gen_" + parts[-1]
        name = "/".join(parts)
        try:
            target = _safe_injection_target(workspace, name)
        except (OSError, ValueError):
            return None
        if target.exists():
            return None
    return name


def _build_gen_test_prompts(repo: Path, head: str | None, test_cmd: str,
                            diff: str, files: list[ChangedFile],
                            source_root: Path | None = None,
                            ) -> tuple[str, str]:
    system = (
        "You write ONE discriminating test file for a code change: it must "
        "FAIL on the codebase as it was before the change and PASS after it. "
        "Use the repository's existing test framework and conventions, and a "
        "filename the test runner will discover. The file must be "
        "self-contained apart from imports from the repository: no network, "
        "no writes outside temp dirs, no changes to other files. Test the "
        "changed behavior itself, not implementation details like exact "
        "wording, so a correct alternative implementation would also pass.")
    sources = [f for f in files if f.subsystem not in ("tests", "docs")
               and f.status != "D"][:MAX_GEN_CONTEXT_FILES]
    blocks = []
    for f in sources:
        if source_root is None:
            content = _head_file_content(repo, head, f.path)
        else:
            try:
                root = source_root.resolve(strict=True)
                candidate = (source_root / f.path).resolve(strict=True)
                candidate.relative_to(root)
                content = candidate.read_text() if candidate.is_file() else None
            except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
                content = None
        if content:
            blocks.append(f"### {f.path} (after the change)\n```\n"
                          f"{content[:MAX_GEN_FILE_CHARS]}\n```")
    existing_tests = [f.strip() for f in _git(
        repo, "ls-files", "*test*", "*spec*", check=False).splitlines()][:20]
    user = (f"# Diff under review\n\n```diff\n{diff[:MAX_DIFF_CHARS]}\n```\n\n"
            f"# Changed files after the change\n\n" + "\n\n".join(blocks) +
            "\n\n# Existing test files (for conventions)\n\n"
            + ("\n".join(f"- {t}" for t in existing_tests) or "- (none)") +
            f"\n\n# Test command that will run your file\n\n`{test_cmd}`\n\n"
            "Write the test file now.")
    return system, user


def run_generated_test_graders(repo: Path, head: str | None, anchor: str,
                               test_cmd: str, diff: str,
                               files: list[ChangedFile], head_ws: Path,
                               out_dir: Path,
                               prompts: tuple[str, str] | None = None,
                               ) -> tuple[list[GraderResult], GeneratedTest | None]:
    """Classical grader with LLM-authored tests: generate a discriminating
    test, run it on head (must pass; one adaptive repair on failure), then
    replay it against the base commit (must fail there)."""
    from .evaluators.judge import structured_completion
    from .graders import _run_shell

    results: list[GraderResult] = []
    console.print("generating a discriminating test with the LLM...")
    system, user = prompts or _build_gen_test_prompts(
        repo, head, test_cmd, diff, files
    )
    try:
        gen, model = structured_completion(system, user, GeneratedTest,
                                           _GEN_TEST_SCHEMA)
    except Exception as e:
        results.append(GraderResult(
            name="generated test", category="classical", blocking=False,
            passed=None, details=f"test generation failed: {e}"))
        return results, None

    filename = _sanitize_gen_filename(gen.filename, head_ws)
    if filename is None:
        results.append(GraderResult(
            name="generated test", category="classical", blocking=False,
            passed=None, details=f"unusable generated filename: {gen.filename!r}"))
        return results, None
    gen.filename = filename

    def run_with_injected(tree: Path, code: str) -> tuple[int | None, str]:
        try:
            target = _write_injected_file(
                tree, filename, code, overwrite=False
            )
        except (OSError, ValueError) as exc:
            return None, f"generated test injection refused: {exc}"
        try:
            return _run_shell(test_cmd, tree, TEST_TIMEOUT)
        finally:
            target.unlink(missing_ok=True)

    start = time.monotonic()
    code_exit, tail = run_with_injected(head_ws, gen.code)
    adapted = False
    if code_exit not in (0, None):
        # adaptive grading: one repair pass for superficial interface mismatch
        console.print("generated test failed on head; one adaptive repair...")
        repair_system = (
            "Your generated test failed against the changed (head) code. If "
            "the failure is a superficial mismatch with the actual interface "
            "(names, signatures, fixtures), fix the test. If it reveals a "
            "real behavioral bug in the change, keep the failing assertion "
            "and say so in notes. Output the complete corrected file.")
        # Runtime output may contain ambient host secrets printed by the
        # repository or test process. It is retained as local evidence but is
        # never included in an external-model repair prompt.
        repair_user = (
            f"{user}\n\n# Your previous test file ({gen.filename})\n\n"
            f"```\n{gen.code}\n```\n\n# Failure status\n\n"
            "The test command exited non-zero. Runtime output is intentionally "
            "withheld; repair only superficial mismatches visible in the "
            "provided code and diff."
        )
        try:
            gen2, model = structured_completion(repair_system, repair_user,
                                                GeneratedTest, _GEN_TEST_SCHEMA)
            gen.code, gen.notes, adapted = gen2.code, gen2.notes, True
            code_exit, tail = run_with_injected(head_ws, gen.code)
        except Exception as e:
            console.print(f"[yellow]adaptive repair failed: {e}[/yellow]")

    gen_dir = out_dir / "generated-tests"
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / Path(filename).name).write_text(gen.code)

    label = "generated test on head" + (" (adapted)" if adapted else "")
    if code_exit is None:
        head_pass, details = None, tail
    elif code_exit == 0:
        head_pass, details = True, f"{filename} passes against the change"
    else:
        head_pass = False
        details = (f"{filename} FAILS against the change"
                   + (" after one adaptation" if adapted else "")
                   + "; possible behavioral bug (see generated-tests/ and output)")
    results.append(GraderResult(name=label, category="classical",
                                blocking=False, weight=2, passed=head_pass,
                                details=details, output_tail=tail,
                                duration_s=round(time.monotonic() - start, 1)))

    if head_pass:
        start = time.monotonic()
        with worktree(repo, anchor) as base_tree:
            baseline_exit, baseline_tail = _run_shell(
                test_cmd, base_tree, TEST_TIMEOUT
            )
            if baseline_exit == 0:
                base_exit, base_tail = run_with_injected(base_tree, gen.code)
            else:
                base_exit, base_tail = None, baseline_tail
        if baseline_exit is None:
            base_pass = None
            details = (
                "base suite timed out before generated-test injection; "
                "discrimination could not be established"
            )
        elif baseline_exit != 0:
            base_pass = None
            details = (
                f"base suite already fails (exit {baseline_exit}) before "
                "generated-test injection; discrimination could not be established"
            )
        elif base_exit is None:
            base_pass, details = None, base_tail
        elif base_exit != 0:
            base_pass = True
            details = f"{filename} fails on the base commit, as it should"
        else:
            base_pass = False
            details = (f"{filename} also passes on the base commit; the "
                       "change may be non-behavioral or the test is weak")
        results.append(GraderResult(
            name="generated test vs base commit", category="reverse-classical",
            blocking=False, weight=1, passed=base_pass, details=details,
            output_tail=base_tail,
            duration_s=round(time.monotonic() - start, 1)))
    return results, gen


# ------------------------------------------------------------------ LLM review

def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def _diff_evidence_runs(
    diff: str, changed_paths: list[str]
) -> dict[str, dict[str, list[list[tuple[int, int, str]]]]]:
    """Return contiguous added/deleted runs with source lines and head anchors."""

    known_paths = set(changed_paths)
    runs: dict[str, dict[str, list[list[tuple[int, int, str]]]]] = {
        side: {path: [] for path in changed_paths} for side in ("head", "base")
    }
    base_path: str | None = None
    head_path: str | None = None
    base_line: int | None = None
    head_line: int | None = None
    head_run: list[tuple[int, int, str]] | None = None
    base_run: list[tuple[int, int, str]] | None = None

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            base_path = head_path = None
            base_line = head_line = None
            head_run = base_run = None
            continue
        if base_line is None and line.startswith("--- "):
            base_path = _patch_path(line[4:], known_paths)
            continue
        if base_line is None and line.startswith("+++ "):
            head_path = _patch_path(line[4:], known_paths)
            continue

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            base_line = int(hunk.group("base"))
            head_line = int(hunk.group("head"))
            head_run = base_run = None
            continue
        if base_line is None or head_line is None:
            continue
        if line.startswith("+"):
            base_run = None
            if head_path is not None:
                if head_run is None:
                    head_run = []
                    runs["head"][head_path].append(head_run)
                head_run.append((head_line, max(1, head_line), line[1:]))
            head_line += 1
        elif line.startswith("-"):
            head_run = None
            if base_path is not None:
                if base_run is None:
                    base_run = []
                    runs["base"][base_path].append(base_run)
                base_run.append((base_line, max(1, head_line), line[1:]))
            base_line += 1
        elif line.startswith("\\ No newline"):
            continue
        else:
            head_run = base_run = None
            base_line += 1
            head_line += 1

    return runs


def _evidence_line_ranges(
    runs: list[list[tuple[int, int, str]]], quote: str
) -> list[tuple[int, int, int]]:
    """Locate a normalized quote and return source range plus head anchor."""

    matches: list[tuple[int, int, int]] = []
    for run in runs:
        text = ""
        spans: list[tuple[int, int, int, int]] = []
        for line_number, head_anchor, content in run:
            normalized = _normalize_ws(content)
            if not normalized:
                continue
            if text:
                text += " "
            start = len(text)
            text += normalized
            spans.append((start, len(text), line_number, head_anchor))

        offset = 0
        while quote and (position := text.find(quote, offset)) >= 0:
            end = position + len(quote)
            matched_lines = [
                line_number
                for start, stop, line_number, _ in spans
                if start < end and stop > position
            ]
            if matched_lines:
                anchors = [
                    head_anchor
                    for start, stop, _, head_anchor in spans
                    if start < end and stop > position
                ]
                location = (matched_lines[0], matched_lines[-1], anchors[0])
                if location not in matches:
                    matches.append(location)
            offset = position + 1
    return matches


def _finding_path(value: str, changed_paths: set[str]) -> str | None:
    """Resolve an exact or unambiguous shortened repository path."""
    raw = value.strip()
    if raw in changed_paths:
        return raw
    matches = [path for path in changed_paths if path.endswith(f"/{raw}")]
    return matches[0] if len(matches) == 1 else None


def verify_findings(findings: list[Finding], diff: str,
                    changed_paths: list[str]) -> list[Finding]:
    """Programmatic verification bar: a finding survives only if its evidence
    is a whitespace-normalized quote from contiguous added or deleted lines in
    its named changed file. A supplied line must overlap that evidence on the
    declared side. Base-side findings are mapped to the nearest head hunk
    anchor for reporting. Nits are capped at MAX_NITS."""
    known_paths = set(changed_paths)
    evidence_runs = _diff_evidence_runs(diff, changed_paths)
    kept: list[Finding] = []
    nits = 0
    for f in findings:
        quote = _normalize_ws(f.evidence)
        path = _finding_path(f.file, known_paths)
        side = f.evidence_side if f.evidence_side in {"head", "base"} else None
        locations = (
            _evidence_line_ranges(evidence_runs[side].get(path, []), quote)
            if path and side and len(quote) >= 12
            else []
        )
        supplied_line = f.line
        if supplied_line is None and locations:
            supplied_line = locations[0][0]
        matching = next(
            (
                location
                for location in locations
                if supplied_line is not None
                and location[0] <= supplied_line <= location[1]
            ),
            None,
        )
        f.verified = matching is not None
        if matching is not None:
            f.evidence_line = supplied_line
            f.line = matching[2]
        if f.verified and f.severity == "nit":
            nits += 1
            if nits > MAX_NITS:
                f.verified = False
                f.verdict_reason = "nit cap exceeded"
        kept.append(f)
    return kept


def _grader_evidence_lines(graders: list[GraderResult]) -> list[str]:
    lines = []
    for g in graders:
        state = {True: "pass", False: "FAIL", None: "skipped"}[g.passed]
        lines.append(f"  - [{state}] {g.name} ({g.category}): {g.details}")
        # Command output stays in the local report. A repository can print
        # ambient secrets, so output tails do not cross the model boundary.
    return lines


def _build_review_prompts(report: ChangeReport, diff: str,
                          context: str | None) -> tuple[str, str]:
    system = (
        "You are a senior engineer writing a pre-merge risk review of a code "
        "change. You are given executable evidence (grader results, scanner "
        "findings, risk signals) plus the diff.\n"
        "Rules for findings:\n"
        "- Every finding's `evidence` must be a contiguous quote from changed "
        "lines in its named file. Set `evidence_side` to `head` for added lines "
        "or `base` for deleted lines. Its `line` must be on the quoted side; "
        "use null to derive it from the evidence. Whitespace is normalized, "
        "but paraphrases are rejected. Findings "
        "whose quote cannot be located are discarded, so never paraphrase.\n"
        "- Report only problems INTRODUCED by this change, not pre-existing "
        "ones, and only what you can prove from the diff and evidence; if "
        "you cannot prove it, it is not a finding.\n"
        "- Severity: blocker = merging this breaks behavior or security; "
        "major = likely bug or security weakness needing action before "
        "merge; minor = worth fixing, not gating; nit = style. At most "
        f"{MAX_NITS} nits; no style nits unless they hide a bug.\n"
        "- A clean change gets an empty findings list. Do not invent issues "
        "to seem thorough.\n"
        "Rate overall risk low/medium/high from blast radius, reversibility, "
        "and the evidence.")
    file_lines = "\n".join(f"- {f.path} ({f.subsystem}, {f.status}, "
                           f"+{f.lines_added}/-{f.lines_removed})"
                           for f in report.files) or "- (none)"
    evidence = [f"Risk signals: {'; '.join(report.signals) or 'none'}"]
    if report.graders:
        evidence.append("Graders:")
        evidence += _grader_evidence_lines(report.graders)
    if report.scans:
        s = report.scans
        evidence.append(f"Scanners: lint_errors={s.lint_errors} "
                        f"security_findings(high/med/low)={s.sec_findings_high}/"
                        f"{s.sec_findings_medium}/{s.sec_findings_low} "
                        f"secrets={s.secrets_found} dep_vulns={s.vulns}")
        for f in s.findings[:20]:
            evidence.append(f"  - [{f.get('severity')}] {f.get('rule')} "
                            f"at {f.get('path')}:{f.get('line')}")
    user = ""
    if context:
        user += f"# Intent (ticket/spec the change should implement)\n\n{context}\n\n"
    user += (f"# Changed files\n\n{file_lines}\n\n"
             f"# Evidence\n\n" + "\n".join(evidence) + "\n\n"
             f"# Diff\n\n```diff\n{diff}\n```\n\n"
             "Produce: overall risk, a summary of what changed (bullets), "
             "findings per the rules above, what a human reviewer should "
             "focus on, and specific missing tests worth adding.")
    return system, user


def _llm_verify_findings(findings: list[Finding], diff: str) -> None:
    """Adversarial second pass over verified blocker/major findings: each must
    be re-confirmed against the diff or it is rejected. Mutates verdicts."""
    from .evaluators.judge import structured_completion

    candidates = [(i, f) for i, f in enumerate(findings)
                  if f.verified and f.severity in ("blocker", "major")]
    if not candidates:
        return
    system = (
        "You are an adversarial verifier for code-review findings. For each "
        "candidate finding, answer CONFIRMED only if the diff proves the "
        "claim: the quoted code, as changed, would actually cause the stated "
        "problem. REJECT speculation, style preferences framed as bugs, "
        "problems that existed before this change, and claims that depend on "
        "code not visible in the diff. Rejecting a wrong finding is as "
        "valuable as confirming a real one.")
    listing = "\n".join(
        f"[{i}] ({f.severity}/{f.category}) {f.file}:{f.line} — {f.claim}\n"
        f"    evidence ({f.evidence_side} line {f.evidence_line}): {f.evidence}"
        for i, f in candidates)
    user = (f"# Candidate findings\n\n{listing}\n\n"
            f"# Diff\n\n```diff\n{diff}\n```\n\n"
            "Return a verdict for every candidate index above.")
    console.print(f"verifying {len(candidates)} blocker/major finding(s)...")
    try:
        parsed, _ = structured_completion(system, user, _VerdictResponse,
                                          _VERDICTS_SCHEMA)
    except Exception as e:
        console.print(f"[yellow]finding verification failed: {e}; "
                      "keeping findings unconfirmed[/yellow]")
        return
    by_index = dict(candidates)
    for v in parsed.verdicts:
        f = by_index.get(v.index)
        if f is not None:
            f.verdict = v.verdict if v.verdict in ("confirmed", "rejected") \
                else "rejected"
            f.verdict_reason = v.reason


def llm_findings_risk(review: LLMReview) -> str:
    """Risk contribution of the LLM review: derived only from findings that
    survived both verification passes, never from unsupported opinion."""
    active = [f for f in review.findings if f.active]
    if any(f.severity == "blocker" and f.verdict == "confirmed" for f in active):
        return "high"
    if any(f.severity == "major" and f.verdict == "confirmed" for f in active):
        return "high"
    return "low"


def run_llm_review(report: ChangeReport, diff: str,
                   context: str | None) -> tuple[LLMReview | None, str | None]:
    from .evaluators.judge import pick_backend, structured_completion

    backend = pick_backend()
    if backend is None:
        console.print("[yellow]no LLM backend available (need ANTHROPIC_API_KEY "
                      "or a logged-in codex CLI); skipping LLM review[/yellow]")
        return None, None
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated for review]"
    system, user = _build_review_prompts(report, diff, context)
    console.print(f"LLM risk review with [bold]{backend}[/bold] backend...")
    try:
        parsed, model = structured_completion(system, user, LLMReview,
                                              _REVIEW_SCHEMA)
    except Exception as e:  # LLM review is supplementary; never fail the report
        console.print(f"[yellow]LLM review failed: {e}[/yellow]")
        return None, None

    changed_paths = [f.path for f in report.files]
    parsed.findings = verify_findings(parsed.findings, diff, changed_paths)
    parsed.dropped_unverified = sum(1 for f in parsed.findings if not f.verified)
    if parsed.dropped_unverified:
        console.print(f"[yellow]{parsed.dropped_unverified} finding(s) failed "
                      "evidence verification and will not affect risk[/yellow]")
    try:
        _llm_verify_findings(parsed.findings, diff)
    except Exception as e:
        console.print(f"[yellow]verification pass failed: {e}[/yellow]")
    return parsed, model


# ------------------------------------------------------------------- pipeline

def review_change(repo: Path, base: str | None = None, head: str | None = None,
                  *, test_cmd: str | None = None, context: str | None = None,
                  checks: list[str] | None = None, gen_tests: bool = False,
                  policy_path: Path | None = None,
                  run_scans: bool = True, run_llm: bool = True,
                  out_dir: Path | None = None,
                  allow_local_execution: bool = False) -> ChangeReport:
    repo = repo.resolve()
    base = resolve_base(repo, base)
    policy = load_policy(
        repo,
        policy_path,
        trusted_ref=base if policy_path is None else None,
    )
    test_cmd = test_cmd or policy.test_cmd
    checks = [*policy.checks, *(checks or [])]
    if (test_cmd or checks or gen_tests) and not allow_local_execution:
        raise RuntimeError(
            "local execution blocked: test/check/generated graders run "
            "change-controlled code; pass allow_local_execution only for a "
            "trusted change"
        )
    report = ChangeReport(repo=str(repo), base=base,
                          head=head or "working tree", created_at=now_iso())

    files, stats, diff_text = collect_changes(repo, base, head)
    report.files, report.diff = files, stats
    anchor = _merge_base(repo, base, head or "HEAD")
    if out_dir is None:
        out_dir = repo / ".agent-eval" / "reviews" / time.strftime("%Y%m%d-%H%M%S")
        # keep our reports out of the user's version control
        (repo / ".agent-eval").mkdir(exist_ok=True)
        (repo / ".agent-eval" / ".gitignore").write_text("*\n")
    out_dir.mkdir(parents=True, exist_ok=True)
    report.report_dir = str(out_dir)
    (out_dir / "change.diff").write_text(diff_text)

    if not files:
        report.signals = [f"no changes between {base} and {report.head}"]
        _persist(report, out_dir)
        return report

    report.graders += scope_graders([(f.path, f.subsystem) for f in files],
                                    stats.lines_added + stats.lines_removed,
                                    policy)
    if policy_path is None and any(
        file.path in {".agent-eval.yaml", ".agent-eval.yml"} for file in files
    ):
        report.graders.append(
            GraderResult(
                name="review policy is unchanged",
                category="scope",
                blocking=True,
                passed=False,
                details=(
                    "the change modifies its own review policy; review that "
                    "policy separately or supply an external --policy"
                ),
            )
        )

    secret_screening_safe = False
    generated_test_prompts: tuple[str, str] | None = None
    if run_scans:
        with tempfile.TemporaryDirectory(prefix="agent-eval-review-") as tmp:
            scan_root = Path(tmp)
            copied = snapshot_changed_files(repo, files, head, scan_root)
            from .evaluators.scanners import run_scanners

            # Screen the exact diff, including deleted lines and filename
            # metadata, in addition to the head-side source snapshot.
            (scan_root / "agent-eval-change.diff").write_text(diff_text)
            metadata = [
                context or "",
                test_cmd or "",
                *checks,
                policy.model_dump_json(),
                *(file.path for file in files),
                *_git(
                    repo, "ls-files", "*test*", "*spec*", check=False
                ).splitlines()[:20],
            ]
            (scan_root / "agent-eval-review-metadata.txt").write_text(
                "\n".join(metadata) + "\n"
            )
            language = "python" if any(f.path.endswith(".py") for f in files) else None
            console.print(f"scanning {copied} changed file(s)...")
            report.scans = run_scanners(scan_root, out_dir, language)
            raw_secret_count = report.scans.secrets_found
            secret_screening_safe = (
                report.scans.scanner_status.get("gitleaks") == "ok"
                and raw_secret_count == 0
            )
            if secret_screening_safe and gen_tests and test_cmd and run_llm:
                # Cache model input from the screened snapshot before any
                # change-controlled local command can mutate the worktree.
                generated_test_prompts = _build_gen_test_prompts(
                    repo,
                    head,
                    test_cmd,
                    diff_text,
                    files,
                    source_root=scan_root,
                )
            prefix = str(scan_root.resolve()) + "/"
            for finding in report.scans.findings:
                if isinstance(finding.get("path"), str):
                    finding["path"] = finding["path"].removeprefix(prefix)
            _scope_scans_to_changed_lines(report.scans, files)
            if raw_secret_count is not None:
                report.scans.secrets_found = raw_secret_count

    report.graders += scanner_graders(report.scans, policy)
    external_model_safe = secret_screening_safe
    if run_llm or gen_tests:
        report.graders.append(
            GraderResult(
                name="external model input has passed secret screening",
                category="scanner",
                blocking=True,
                passed=external_model_safe,
                details=(
                    "gitleaks completed with zero secrets"
                    if external_model_safe
                    else "external model calls skipped: gitleaks must complete "
                    "successfully with zero detected secrets"
                ),
            )
        )

    head_tests_pass: bool | None = None
    with head_workspace(repo, head) as ws:
        for cmd in checks:
            console.print(f"command grader: [bold]{cmd}[/bold]")
            report.graders.append(command_grader(cmd, ws))
        if test_cmd:
            console.print(f"classical grader (tests on head): [bold]{test_cmd}[/bold]")
            result = head_test_grader(test_cmd, ws)
            report.graders.append(result)
            head_tests_pass = result.passed
        if gen_tests:
            if not test_cmd:
                console.print("[yellow]--gen-tests needs --test-cmd (or "
                              "test_cmd in policy); skipping[/yellow]")
            elif head_tests_pass is not True:
                console.print("[yellow]skipping generated tests: the existing "
                              "suite must pass on head first so failures are "
                              "attributable[/yellow]")
            elif (
                run_llm
                and external_model_safe
                and generated_test_prompts is not None
            ):
                gen_results, _ = run_generated_test_graders(
                    repo,
                    head,
                    anchor,
                    test_cmd,
                    diff_text,
                    files,
                    ws,
                    out_dir,
                    prompts=generated_test_prompts,
                )
                report.graders += gen_results
            else:
                console.print(
                    "[yellow]skipping generated tests: diff did not pass "
                    "secret screening[/yellow]"
                )

    changed_tests = {f.path: c for f in files
                     if f.subsystem == "tests" and f.status != "D"
                     and (c := _head_file_content(repo, head, f.path)) is not None}
    if test_cmd and changed_tests:
        console.print("reverse-classical grader: replaying new/changed tests "
                      "against the base commit...")
        with worktree(repo, anchor) as base_tree:
            report.graders.append(
                reverse_test_grader(test_cmd, base_tree, changed_tests))
    elif test_cmd:
        report.graders.append(reverse_test_grader(test_cmd, repo, {}))

    report.signals, report.heuristic_risk = compute_risk(
        files, stats, report.scans, None, report.graders)
    report.blocked = any(g.blocking and g.passed is False for g in report.graders)
    report.risk = report.heuristic_risk

    if run_llm and external_model_safe:
        report.llm, report.llm_model = run_llm_review(report, diff_text, context)
        if report.llm:
            report.risk = _max_risk(report.heuristic_risk,
                                    llm_findings_risk(report.llm))
    elif run_llm:
        console.print(
            "[yellow]skipping LLM review: diff did not pass secret screening[/yellow]"
        )

    _persist(report, out_dir)
    return report


def _persist(report: ChangeReport, out_dir: Path) -> None:
    from .sarif import write_sarif

    (out_dir / "review.json").write_text(report.model_dump_json(indent=2))
    (out_dir / "review.md").write_text(markdown_review(report))
    write_sarif(report, out_dir / "review.sarif")


# ------------------------------------------------------------------- rendering

_RISK_COLOR = {"low": "green", "medium": "yellow", "high": "red"}
_GRADER_STATE = {True: "[green]pass[/green]", False: "[red]FAIL[/red]",
                 None: "[dim]skipped[/dim]"}


def _active_findings(report: ChangeReport) -> list[Finding]:
    return [f for f in report.llm.findings if f.active] if report.llm else []


def print_review(report: ChangeReport) -> None:
    color = _RISK_COLOR.get(report.risk, "white")
    console.rule(f"[bold {color}]Risk: {report.risk.upper()}[/bold {color}]  "
                 f"({report.base} → {report.head})"
                 + ("  [red]BLOCKED[/red]" if report.blocked else ""))

    table = Table(title=None, show_edge=False, pad_edge=False)
    for col in ("file", "subsystem", "status", "+/-"):
        table.add_column(col)
    for f in report.files[:40]:
        table.add_row(f.path, f.subsystem, f.status,
                      f"+{f.lines_added}/-{f.lines_removed}")
    if len(report.files) > 40:
        table.add_row(f"... {len(report.files) - 40} more", "", "", "")
    console.print(table)
    console.print(f"\n{report.diff.files_changed} file(s), "
                  f"+{report.diff.lines_added}/-{report.diff.lines_removed}")

    if report.signals:
        console.print("\n[bold]Signals[/bold] "
                      f"(heuristic risk: {report.heuristic_risk})")
        for s in report.signals:
            console.print(f"  • {s}")
    if report.scans:
        s = report.scans
        console.print(f"\n[bold]Scans[/bold]  lint={s.lint_errors} "
                      f"sec high/med/low={s.sec_findings_high}/"
                      f"{s.sec_findings_medium}/{s.sec_findings_low} "
                      f"secrets={s.secrets_found} vulns={s.vulns}")
        console.print(
            "  statuses: "
            + ", ".join(
                f"{name}={status}"
                for name, status in sorted(s.scanner_status.items())
            )
        )
    if report.graders:
        gt = Table(title=None, show_edge=False, pad_edge=False)
        for col in ("grader", "category", "result", "details"):
            gt.add_column(col)
        for g in report.graders:
            gt.add_row(g.name, g.category, _GRADER_STATE[g.passed],
                       g.details[:100])
        console.print("\n[bold]Graders[/bold]")
        console.print(gt)

    if report.llm:
        console.print(f"\n[bold]LLM review[/bold] ({report.llm_model}; "
                      f"holistic risk: {report.llm.risk}, counted risk: "
                      f"{llm_findings_risk(report.llm)})")
        for section, items in (("Summary", report.llm.summary),
                               ("Reviewer focus", report.llm.reviewer_focus),
                               ("Missing tests", report.llm.missing_tests)):
            if items:
                console.print(f"  [underline]{section}[/underline]")
                for item in items:
                    console.print(f"    • {item}")
        active = _active_findings(report)
        if active:
            console.print("  [underline]Findings (evidence-verified)[/underline]")
            for f in active:
                mark = f" ({f.verdict})" if f.verdict else ""
                console.print(f"    • {f.severity.upper()}: {f.file}"
                              + (f":{f.line}" if f.line else "")
                              + f" — {f.claim}{mark}", markup=False)
        dropped = report.llm.dropped_unverified
        rejected = sum(1 for f in report.llm.findings
                       if f.verified and f.verdict == "rejected")
        if dropped or rejected:
            console.print(f"  [dim]{dropped} finding(s) dropped (no verbatim "
                          f"evidence), {rejected} rejected on verification[/dim]")
    console.print(f"\nreport: {report.report_dir}/review.md")


def markdown_review(report: ChangeReport) -> str:
    lines = [f"# Change review: {report.base} → {report.head}",
             "",
             f"**Risk: {report.risk.upper()}**"
             + (" **(BLOCKED)**" if report.blocked else "") + "  "
             f"(heuristic: {report.heuristic_risk}"
             + (f", LLM findings: {llm_findings_risk(report.llm)}"
                if report.llm else "") + ")",
             "",
             f"- Repo: `{report.repo}`",
             f"- Generated: {report.created_at}",
             f"- Diff: {report.diff.files_changed} file(s), "
             f"+{report.diff.lines_added}/-{report.diff.lines_removed}",
             "",
             "## Changed files", ""]
    for f in report.files:
        lines.append(f"- `{f.path}` ({f.subsystem}, {f.status}, "
                     f"+{f.lines_added}/-{f.lines_removed})")
    lines += ["", "## Risk signals", ""]
    lines += [f"- {s}" for s in report.signals] or ["- none"]
    if report.scans:
        s = report.scans
        lines += ["", "## Scanners", "",
                  f"- lint errors: {s.lint_errors}",
                  f"- security findings (high/med/low): {s.sec_findings_high}/"
                  f"{s.sec_findings_medium}/{s.sec_findings_low}",
                  f"- secrets: {s.secrets_found}",
                  f"- dependency vulns: {s.vulns}"]
    if report.graders:
        lines += ["", "## Graders", "",
                  "| grader | category | result | details |",
                  "|---|---|---|---|"]
        state = {True: "pass", False: "**FAIL**", None: "skipped"}
        for g in report.graders:
            details = g.details.replace("|", "/")
            lines.append(f"| {g.name} | {g.category} | {state[g.passed]} | "
                         f"{details} |")
    if report.llm:
        lines += ["", f"## LLM review ({report.llm_model})", ""]
        for title, items in (("Summary", report.llm.summary),
                             ("Recommended reviewer focus", report.llm.reviewer_focus),
                             ("Missing tests", report.llm.missing_tests)):
            if items:
                lines += [f"### {title}", ""] + [f"- {i}" for i in items] + [""]
        active = _active_findings(report)
        if active:
            lines += ["### Findings (evidence-verified)", ""]
            for f in active:
                loc = f.file + (f":{f.line}" if f.line else "")
                mark = f" *({f.verdict})*" if f.verdict else ""
                lines += [f"- **{f.severity}** `{loc}` — {f.claim}{mark}",
                          f"  - evidence: `{f.evidence[:200]}`"]
            lines.append("")
        dropped = report.llm.dropped_unverified
        rejected = sum(1 for f in report.llm.findings
                       if f.verified and f.verdict == "rejected")
        if dropped or rejected:
            lines += [f"*{dropped} finding(s) dropped without matching diff "
                      f"evidence; {rejected} rejected on adversarial "
                      "verification (see review.json).*", ""]
    return "\n".join(lines) + "\n"
