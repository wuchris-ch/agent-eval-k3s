import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_eval.evaluators import scanners
from agent_eval.metrics import ScanResults


def test_ruff_uses_pinned_package_and_accepts_json_list(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, out_file):
        captured["cmd"] = cmd
        captured["out_file"] = out_file
        return SimpleNamespace(returncode=0, stdout='[{"code": "F401"}]'), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._lint("python", tmp_path / "workspace", tmp_path, results)

    assert captured["cmd"][:4] == [
        "uvx",
        "--from",
        "ruff==0.15.20",
        "ruff",
    ]
    assert captured["out_file"] == tmp_path / "ruff.json"
    assert results.scanner_status["ruff"] == "ok"
    assert results.lint_errors == 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "[]"),
        (2, "[]"),
        (0, "not-json"),
        (0, '{"findings": []}'),
    ],
)
def test_ruff_errors_leave_metric_unset(
    monkeypatch, tmp_path, returncode, stdout
):
    def fake_run(cmd, out_file):
        del cmd, out_file
        return SimpleNamespace(returncode=returncode, stdout=stdout), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._lint("python", tmp_path / "workspace", tmp_path, results)

    assert results.scanner_status["ruff"] == "error"
    assert results.lint_errors is None


def test_semgrep_command_identifies_version_and_config(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, out_file):
        captured["cmd"] = cmd
        captured["out_file"] = out_file
        return SimpleNamespace(returncode=0, stdout='{"results": []}'), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._semgrep(tmp_path / "workspace", tmp_path, results)

    assert captured["cmd"][:6] == [
        "uvx",
        "--python",
        "3.12",
        "--from",
        "semgrep==1.169.0",
        "semgrep",
    ]
    assert captured["cmd"][captured["cmd"].index("--config") + 1] == "auto"
    assert captured["cmd"][captured["cmd"].index("--metrics") + 1] == "auto"
    assert captured["out_file"] == tmp_path / "semgrep.json"
    assert results.scanner_status["semgrep"] == "ok"
    assert results.sec_findings_high == 0


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "[]",
        '{"results": {}}',
        '{"results": ["bad"]}',
        '{"results": [{"extra": {"severity": []}}]}',
        '{"results": [{"start": "bad"}]}',
    ],
)
def test_semgrep_malformed_report_leaves_metrics_unset(monkeypatch, tmp_path, stdout):
    def fake_run(cmd, out_file):
        del cmd, out_file
        return SimpleNamespace(returncode=0, stdout=stdout), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._semgrep(tmp_path / "workspace", tmp_path, results)

    assert results.scanner_status["semgrep"] == "error"
    assert results.sec_findings_high is None
    assert results.sec_findings_medium is None
    assert results.sec_findings_low is None


@pytest.mark.parametrize(
    ("returncode", "report_contents"),
    [
        (2, "[]"),
        (0, None),
        (0, "not-json"),
        (0, b"\xff"),
        (1, '{"findings": []}'),
    ],
)
def test_gitleaks_errors_leave_metric_unset(
    monkeypatch, tmp_path, returncode, report_contents
):
    workspace = tmp_path / "workspace"
    scans_dir = tmp_path / "scans"
    workspace.mkdir()
    scans_dir.mkdir()

    def fake_run(cmd, out_file):
        del out_file
        report = Path(cmd[cmd.index("--report-path") + 1])
        if report_contents is not None:
            if isinstance(report_contents, bytes):
                report.write_bytes(report_contents)
            else:
                report.write_text(report_contents)
        return SimpleNamespace(returncode=returncode), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._gitleaks(workspace, scans_dir, results)

    assert results.scanner_status["gitleaks"] == "error"
    assert results.secrets_found is None
    assert results.findings == []


@pytest.mark.parametrize("returncode", [0, 1])
def test_gitleaks_accepts_valid_empty_list_for_documented_exits(
    monkeypatch, tmp_path, returncode
):
    workspace = tmp_path / "workspace"
    scans_dir = tmp_path / "scans"
    workspace.mkdir()
    scans_dir.mkdir()

    def fake_run(cmd, out_file):
        del out_file
        report = Path(cmd[cmd.index("--report-path") + 1])
        report.write_text(json.dumps([]))
        return SimpleNamespace(returncode=returncode), "ok"

    monkeypatch.setattr(scanners.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(scanners, "_run", fake_run)
    results = ScanResults()

    scanners._gitleaks(workspace, scans_dir, results)

    assert results.scanner_status["gitleaks"] == "ok"
    assert results.secrets_found == 0
    assert json.loads((scans_dir / "gitleaks.json").read_text()) == []
