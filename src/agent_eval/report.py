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
    for col in ("run id", "agent", "outcome", "resolved", "tests", "cov%", "time s", "cost $",
                "tokens", "turns", "diff +/-", "judge"):
        table.add_column(col)
    for r in rows:
        record = RunRecord.model_validate_json(r["results_json"])
        if (
            record.correctness.command_exit_code is None
            and record.correctness.infra_error is None
        ):
            resolved = "[yellow]unknown (legacy)[/yellow]"
        else:
            resolved = (
                "[green]yes[/green]" if record.correctness.resolved
                else "[red]no[/red]"
            )
        outcome = record.outcome.status if record.outcome else "legacy"
        tokens = (f"{r['tokens_in']}/{r['tokens_out']}"
                  if r["tokens_in"] is not None else "-")
        table.add_row(
            r["run_id"], r["agent"], outcome, resolved,
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
    accepted = sum(r.outcome is not None and r.outcome.status == "accepted"
                   for r in records)
    rejected = sum(r.outcome is not None and r.outcome.status == "rejected"
                   for r in records)
    infra = sum(r.outcome is not None and r.outcome.status == "infra_error"
                for r in records)
    console.print(
        f"\ntrials: {n}  resolved: {c}  pass@{k}: {pass_at_k(n, c, k):.2f}  "
        f"outcomes accepted/rejected/infra: {accepted}/{rejected}/{infra}"
    )


def markdown_report(task_id: str | None = None, limit: int = 50) -> str:
    rows = load_runs(task_id, limit)
    lines = ["# agent-eval report", "",
             "| run id | agent | outcome | resolved | tests | cov% | time s | cost $ | tokens in/out | turns | diff | judge |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        record = RunRecord.model_validate_json(r["results_json"])
        has_resolution_evidence = (
            record.correctness.command_exit_code is not None
            or record.correctness.infra_error is not None
        )
        resolved = (
            "yes" if record.correctness.resolved
            else "no" if has_resolution_evidence
            else "unknown (legacy)"
        )
        outcome = record.outcome.status if record.outcome else "legacy"
        tokens = (f"{r['tokens_in']}/{r['tokens_out']}"
                  if r["tokens_in"] is not None else "-")
        lines.append(
            f"| {r['run_id']} | {r['agent']} | {outcome} | {resolved} "
            f"| {_fmt(r['tests_passed'])}/{_fmt(r['tests_total'])} | {_fmt(r['coverage'])} "
            f"| {_fmt(r['wall_time_s'])} | {_fmt(r['cost_usd'])} | {tokens} "
            f"| {_fmt(r['turns'])} | +{r['diff_added']}/-{r['diff_removed']} "
            f"| {_fmt(r['judge_score'])} |")
    return "\n".join(lines) + "\n"
