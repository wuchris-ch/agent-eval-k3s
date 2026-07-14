"""Run records: the results.json schema and the SQLite store for cross-run queries."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .assurance import AssuranceResult
from .evaluators.tests import TestResults
from .outcome import RunOutcome

RUNS_ROOT = Path(__file__).resolve().parents[2] / "runs"


class AgentMetrics(BaseModel):
    """Efficiency metrics parsed from the coding agent's transcript."""
    wall_time_s: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    turns: int | None = None
    tool_calls: int | None = None
    model: str | None = None
    requested_model: str | None = None
    agent_exit_code: int | None = None
    timed_out: bool = False
    infra_error: str | None = None
    runtime_image_digest: str | None = None


class DiffStats(BaseModel):
    lines_added: int = 0
    lines_removed: int = 0
    files_changed: int = 0


class ScanResults(BaseModel):
    lint_errors: int | None = None
    sec_findings_high: int | None = None
    sec_findings_medium: int | None = None
    sec_findings_low: int | None = None
    secrets_found: int | None = None
    vulns: int | None = None
    scanner_status: dict[str, str] = Field(default_factory=dict)
    scanner_versions: dict[str, str | None] = Field(default_factory=dict)
    scanner_configs: dict[str, str] = Field(default_factory=dict)
    findings: list[dict] = Field(default_factory=list)


class JudgeResult(BaseModel):
    scores: dict[str, int] = Field(default_factory=dict)  # dimension -> 1..5
    weighted_score: float | None = None
    rationale: dict[str, str] = Field(default_factory=dict)
    model: str | None = None


class RunProvenance(BaseModel):
    """Non-secret execution identity persisted with a trial."""

    image_tag: str | None = None
    image_digest: str | None = None
    local_image_digest: str | None = None
    task_tree_sha256: str | None = None
    harness_commit: str | None = None
    harness_dirty: bool | None = None
    harness_worktree_sha256: str | None = None
    agent_image_digest: str | None = None
    eval_image_digest: str | None = None
    credential_source: str | None = None
    credential_mode: str | None = None
    credential_expires_at: str | None = None
    tool_versions: dict[str, str | None] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    task_id: str
    agent: str  # adapter name, or "external" for eval-only mode
    trial: int = 1
    experiment_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    correctness: TestResults = Field(default_factory=TestResults)
    efficiency: AgentMetrics = Field(default_factory=AgentMetrics)
    diff: DiffStats = Field(default_factory=DiffStats)
    scans: ScanResults = Field(default_factory=ScanResults)
    judge: JudgeResult = Field(default_factory=JudgeResult)
    assurance: AssuranceResult | None = None
    outcome: RunOutcome | None = None
    provenance: RunProvenance = Field(default_factory=RunProvenance)

    @property
    def run_dir(self) -> Path:
        return RUNS_ROOT / self.run_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    trial INTEGER NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    resolved INTEGER,
    tests_passed INTEGER,
    tests_total INTEGER,
    coverage REAL,
    wall_time_s REAL,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost_usd REAL,
    turns INTEGER,
    tool_calls INTEGER,
    lint_errors INTEGER,
    sec_findings INTEGER,
    secrets_found INTEGER,
    vulns INTEGER,
    diff_added INTEGER,
    diff_removed INTEGER,
    files_changed INTEGER,
    judge_score REAL,
    experiment_id TEXT,
    outcome_status TEXT,
    image_digest TEXT,
    results_json TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    RUNS_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(RUNS_ROOT / "metrics.db")
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    for name in ("experiment_id", "outcome_status", "image_digest"):
        if name not in existing:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT")
    return conn


def save_run(record: RunRecord) -> None:
    record.run_dir.mkdir(parents=True, exist_ok=True)
    (record.run_dir / "results.json").write_text(record.model_dump_json(indent=2))

    c = record.correctness
    s = record.scans
    sec_total = sum(v for v in (s.sec_findings_high, s.sec_findings_medium, s.sec_findings_low)
                    if v is not None) if s.sec_findings_high is not None else None
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs (
               run_id, task_id, agent, trial, started_at, finished_at,
               resolved, tests_passed, tests_total, coverage, wall_time_s,
               tokens_in, tokens_out, cost_usd, turns, tool_calls, lint_errors,
               sec_findings, secrets_found, vulns, diff_added, diff_removed,
               files_changed, judge_score, experiment_id, outcome_status,
               image_digest, results_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.run_id, record.task_id, record.agent, record.trial,
             record.started_at, record.finished_at,
             int(c.resolved), c.passed, c.total, c.coverage_percent,
             record.efficiency.wall_time_s, record.efficiency.tokens_in,
             record.efficiency.tokens_out, record.efficiency.cost_usd,
             record.efficiency.turns, record.efficiency.tool_calls,
             s.lint_errors, sec_total, s.secrets_found, s.vulns,
             record.diff.lines_added, record.diff.lines_removed, record.diff.files_changed,
             record.judge.weighted_score,
             record.experiment_id,
             record.outcome.status if record.outcome else None,
             record.provenance.image_digest,
             record.model_dump_json()),
        )


def load_runs(task_id: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    with _connect() as conn:
        if task_id:
            cur = conn.execute(
                "SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
                (task_id, limit))
        else:
            cur = conn.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        return cur.fetchall()


def load_run(run_id: str) -> RunRecord | None:
    with _connect() as conn:
        row = conn.execute("SELECT results_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return RunRecord.model_validate_json(row["results_json"]) if row else None
