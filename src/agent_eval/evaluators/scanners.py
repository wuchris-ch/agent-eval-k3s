"""Host-side static analysis of the produced workspace. Every scanner degrades
gracefully: a missing tool records None for its metrics rather than failing
the run. Scanner artifacts are kept under runs/<id>/scans/; secret reports
retain only redacted location metadata."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from rich.console import Console

from ..metrics import ScanResults

console = Console()
SCAN_TIMEOUT = 600
_REDACTED_SECRET = "<REDACTED_SECRET>"


def _workspace_source_path(workspace: Path, raw_path: object) -> Path | None:
    source = Path(str(raw_path or ""))
    candidate = source if source.is_absolute() else workspace / source
    try:
        root = workspace.resolve(strict=True)
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _line_identity(lines: list[str], line: object) -> tuple[str, str] | None:
    if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
        return None
    if line > len(lines):
        return None
    source_line = lines[line - 1].strip()
    occurrence = sum(
        candidate_line.strip() == source_line for candidate_line in lines[:line]
    )
    digest = hashlib.sha256(source_line.encode("utf-8")).hexdigest()
    return f"{digest[:16]}:{occurrence}", f"{digest}:{occurrence}"


def _line_fingerprint(lines: list[str], line: object) -> str | None:
    identity = _line_identity(lines, line)
    return identity[0] if identity else None


def _source_line_identity(
    workspace: Path, raw_path: object, line: object
) -> tuple[str, str] | None:
    candidate = _workspace_source_path(workspace, raw_path)
    if candidate is None:
        return None
    try:
        lines = candidate.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return _line_identity(lines, line)


def _source_line_fingerprint(
    workspace: Path, raw_path: object, line: object
) -> str | None:
    identity = _source_line_identity(workspace, raw_path, line)
    return identity[0] if identity else None


def _redacted_gitleaks_identities(
    workspace: Path, findings: list[object]
) -> dict[int, tuple[str, str]]:
    grouped: dict[Path, list[tuple[int, dict]]] = {}
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        source = _workspace_source_path(workspace, finding.get("File"))
        if source is not None:
            grouped.setdefault(source, []).append((index, finding))

    identities: dict[int, tuple[str, str]] = {}
    for source, source_findings in grouped.items():
        try:
            source_lines = source.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        redacted_lines = list(source_lines)

        tokens: dict[int, tuple[str, ...]] = {}
        for index, finding in source_findings:
            secret = finding.get("Secret")
            match = finding.get("Match")
            token = secret if isinstance(secret, str) and secret else match
            if not isinstance(token, str) or not token:
                continue
            segments = tuple(segment for segment in token.splitlines() if segment)
            if segments:
                tokens[index] = segments

        redacted_segments: set[str] = set()
        for segment in sorted(
            {segment for segments in tokens.values() for segment in segments},
            key=len,
            reverse=True,
        ):
            replaced = False
            for line_index, source_line in enumerate(redacted_lines):
                if segment in source_line:
                    redacted_lines[line_index] = source_line.replace(
                        segment, _REDACTED_SECRET
                    )
                    replaced = True
            if replaced:
                redacted_segments.add(segment)

        for index, finding in source_findings:
            finding_tokens = tokens.get(index)
            if not finding_tokens or not all(
                segment in redacted_segments for segment in finding_tokens
            ):
                continue
            line = finding.get("StartLine")
            if (
                isinstance(line, bool)
                or not isinstance(line, int)
                or line <= 0
                or line > len(source_lines)
                or source_lines[line - 1] == redacted_lines[line - 1]
            ):
                continue
            identity = _line_identity(redacted_lines, line)
            if identity:
                identities[index] = identity
    return identities


def _redacted_gitleaks_fingerprints(
    workspace: Path, findings: list[object]
) -> dict[int, str]:
    return {
        index: identity[0]
        for index, identity in _redacted_gitleaks_identities(
            workspace, findings
        ).items()
    }


def _run(cmd: list[str], out_file: Path) -> subprocess.CompletedProcess | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
        out_file.write_text(proc.stdout or proc.stderr)
        return proc
    except (subprocess.TimeoutExpired, OSError) as e:
        console.print(f"[yellow]scanner {cmd[0]} failed: {e}[/yellow]")
        return None


def run_scanners(workspace: Path, run_dir: Path,
                 language: str | None = "python") -> ScanResults:
    results = ScanResults()
    scans_dir = run_dir / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)

    _lint(language, workspace, scans_dir, results)
    _semgrep(workspace, scans_dir, results)
    _gitleaks(workspace, scans_dir, results)
    _trivy(workspace, scans_dir, results)
    return results


def _lint(language: str | None, workspace: Path, scans_dir: Path,
          results: ScanResults) -> None:
    if language != "python":
        return  # eslint etc. can be added per-language later
    proc = _run(["uvx", "ruff", "check", "--output-format", "json", "--exit-zero",
                 str(workspace)], scans_dir / "ruff.json")
    if proc is None:
        return
    try:
        findings = json.loads(proc.stdout)
        results.lint_errors = len(findings)
    except json.JSONDecodeError:
        pass


def _semgrep(workspace: Path, scans_dir: Path, results: ScanResults) -> None:
    proc = _run(["uvx", "--python", "3.12", "semgrep", "scan",
                 "--config", "auto", "--json", "--quiet",
                 str(workspace)], scans_dir / "semgrep.json")
    if proc is None or proc.returncode not in (0, 1):
        return
    try:
        findings = json.loads(proc.stdout).get("results", [])
    except json.JSONDecodeError:
        return
    sev = {"ERROR": 0, "WARNING": 0, "INFO": 0}
    for f in findings:
        sev[f.get("extra", {}).get("severity", "INFO")] = \
            sev.get(f.get("extra", {}).get("severity", "INFO"), 0) + 1
        finding = {
            "tool": "semgrep",
            "rule": f.get("check_id"),
            "severity": f.get("extra", {}).get("severity"),
            "path": f.get("path"),
            "line": (f.get("start") or {}).get("line"),
        }
        line_identity = _source_line_identity(
            workspace, finding["path"], finding["line"]
        )
        if line_identity:
            finding["primary_location_line_hash"] = line_identity[0]
            finding["semantic_location_hash"] = line_identity[1]
        results.findings.append(finding)
    results.sec_findings_high = sev["ERROR"]
    results.sec_findings_medium = sev["WARNING"]
    results.sec_findings_low = sev["INFO"]


def _gitleaks(workspace: Path, scans_dir: Path, results: ScanResults) -> None:
    if not shutil.which("gitleaks"):
        return
    with tempfile.TemporaryDirectory(prefix="agent-eval-gitleaks-") as tmp:
        report = Path(tmp) / "gitleaks.json"
        log = Path(tmp) / "gitleaks.log"
        proc = _run(["gitleaks", "dir", str(workspace), "--report-format", "json",
                     "--report-path", str(report), "--no-banner"],
                    log)
        if proc is None:
            return
        try:
            raw_findings = json.loads(report.read_text()) if report.is_file() else []
        except (json.JSONDecodeError, OSError):
            raw_findings = []
    if not isinstance(raw_findings, list):
        raw_findings = []
    raw_findings = [finding for finding in raw_findings if isinstance(finding, dict)]

    results.secrets_found = len(raw_findings)
    identities = _redacted_gitleaks_identities(workspace, raw_findings)
    retained_findings = []
    for index, raw_finding in enumerate(raw_findings):
        finding = {
            "tool": "gitleaks",
            "rule": raw_finding.get("RuleID") or "secret",
            "severity": "ERROR",
            "path": raw_finding.get("File"),
            "line": raw_finding.get("StartLine"),
        }
        identity = identities.get(index)
        if identity:
            finding["primary_location_line_hash"] = identity[0]
            finding["semantic_location_hash"] = identity[1]
        results.findings.append(finding)
        retained_findings.append(finding)
    (scans_dir / "gitleaks.json").write_text(
        json.dumps(retained_findings, indent=2) + "\n"
    )
    (scans_dir / "gitleaks.log").write_text(
        f"exit_code={proc.returncode} redacted_findings={len(retained_findings)}\n"
    )


def _trivy(workspace: Path, scans_dir: Path, results: ScanResults) -> None:
    if not shutil.which("trivy"):
        return
    proc = _run(["trivy", "fs", "--scanners", "vuln", "--format", "json",
                 str(workspace)], scans_dir / "trivy.json")
    if proc is None or proc.returncode != 0:
        return
    try:
        data = json.loads(proc.stdout)
        results.vulns = sum(len(r.get("Vulnerabilities") or [])
                            for r in data.get("Results") or [])
    except json.JSONDecodeError:
        pass
