"""Terminal and markdown reporting over the SQLite run store."""

from __future__ import annotations

from math import comb

from rich.console import Console
from rich.table import Table

from .metrics import RunRecord, load_run, load_runs

console = Console()


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def print_runs_table(task_id: str | None = None, limit: int = 50) -> None:
    rows = load_runs(task_id, limit)
    if not rows:
        console.print("[yellow]no runs recorded yet[/yellow]")
        return
    table = Table(title="agent-eval runs", show_lines=False)
    for col in ("run id", "agent", "resolved", "tests", "cov%", "time s", "cost $",
                "tokens", "turns", "diff +/-", "judge"):
        table.add_column(col)
    for r in rows:
        resolved = "[green]yes[/green]" if r["resolved"] else "[red]no[/red]"
        tokens = (f"{r['tokens_in']}/{r['tokens_out']}"
                  if r["tokens_in"] is not None else "-")
        table.add_row(
            r["run_id"], r["agent"], resolved,
            f"{_fmt(r['tests_passed'])}/{_fmt(r['tests_total'])}",
            _fmt(r["coverage"]), _fmt(r["wall_time_s"]), _fmt(r["cost_usd"]),
            tokens, _fmt(r["turns"]),
            f"+{r['diff_added']}/-{r['diff_removed']}",
            _fmt(r["judge_score"]),
        )
    console.print(table)


def print_run_detail(run_id: str) -> None:
    record = load_run(run_id)
    if record is None:
        console.print(f"[red]run {run_id} not found[/red]")
        return
    console.print_json(record.model_dump_json())


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator (Chen et al. 2021): n trials, c successes."""
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def print_trial_summary(records: list[RunRecord], k: int = 1) -> None:
    n = len(records)
    c = sum(1 for r in records if r.correctness.resolved)
    console.print(f"\ntrials: {n}  resolved: {c}  pass@{k}: {pass_at_k(n, c, k):.2f}")


def markdown_report(task_id: str | None = None, limit: int = 50) -> str:
    rows = load_runs(task_id, limit)
    lines = ["# agent-eval report", "",
             "| run id | agent | resolved | tests | cov% | time s | cost $ | tokens in/out | turns | diff | judge |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        tokens = (f"{r['tokens_in']}/{r['tokens_out']}"
                  if r["tokens_in"] is not None else "-")
        lines.append(
            f"| {r['run_id']} | {r['agent']} | {'yes' if r['resolved'] else 'no'} "
            f"| {_fmt(r['tests_passed'])}/{_fmt(r['tests_total'])} | {_fmt(r['coverage'])} "
            f"| {_fmt(r['wall_time_s'])} | {_fmt(r['cost_usd'])} | {tokens} "
            f"| {_fmt(r['turns'])} | +{r['diff_added']}/-{r['diff_removed']} "
            f"| {_fmt(r['judge_score'])} |")
    return "\n".join(lines) + "\n"
