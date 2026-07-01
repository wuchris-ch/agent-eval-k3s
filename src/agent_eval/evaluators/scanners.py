"""Host-side static analysis of the produced workspace. Every scanner degrades
gracefully: a missing tool records None for its metrics rather than failing
the run. Raw scanner output is kept under runs/<id>/scans/."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from ..metrics import ScanResults
from ..task import Task

console = Console()
SCAN_TIMEOUT = 600


def _run(cmd: list[str], out_file: Path) -> subprocess.CompletedProcess | None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SCAN_TIMEOUT)
        out_file.write_text(proc.stdout or proc.stderr)
        return proc
    except (subprocess.TimeoutExpired, OSError) as e:
        console.print(f"[yellow]scanner {cmd[0]} failed: {e}[/yellow]")
        return None


def run_scanners(task: Task, workspace: Path, run_dir: Path) -> ScanResults:
    results = ScanResults()
    scans_dir = run_dir / "scans"
    scans_dir.mkdir(parents=True, exist_ok=True)

    _lint(task, workspace, scans_dir, results)
    _semgrep(workspace, scans_dir, results)
    _gitleaks(workspace, scans_dir, results)
    _trivy(workspace, scans_dir, results)
    return results


def _lint(task: Task, workspace: Path, scans_dir: Path, results: ScanResults) -> None:
    if task.language != "python":
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
        results.findings.append({
            "tool": "semgrep",
            "rule": f.get("check_id"),
            "severity": f.get("extra", {}).get("severity"),
            "path": f.get("path"),
            "line": (f.get("start") or {}).get("line"),
        })
    results.sec_findings_high = sev["ERROR"]
    results.sec_findings_medium = sev["WARNING"]
    results.sec_findings_low = sev["INFO"]


def _gitleaks(workspace: Path, scans_dir: Path, results: ScanResults) -> None:
    if not shutil.which("gitleaks"):
        return
    report = scans_dir / "gitleaks.json"
    proc = _run(["gitleaks", "dir", str(workspace), "--report-format", "json",
                 "--report-path", str(report), "--no-banner"], scans_dir / "gitleaks.log")
    if proc is None:
        return
    try:
        results.secrets_found = len(json.loads(report.read_text())) if report.is_file() else 0
    except json.JSONDecodeError:
        results.secrets_found = 0


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
