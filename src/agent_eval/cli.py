"""agent-eval CLI: cluster lifecycle, task management, runs, and reports."""

from __future__ import annotations

import math
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import cluster as cluster_mod
from .report import markdown_report, print_run_detail, print_runs_table, print_trial_summary
from .runner import evaluate_workspace, validate_task
from .task import list_tasks, load_task

app = typer.Typer(
    help="Change-assurance and coding-agent evaluation harness. "
         "`review` works on any git repo with no setup; `run` benchmarks "
         "agents inside k3s sandboxes.",
    no_args_is_help=True)
cluster_app = typer.Typer(help="Manage the k3d/k3s cluster.", no_args_is_help=True)
tasks_app = typer.Typer(help="List and validate eval tasks.", no_args_is_help=True)
corpus_app = typer.Typer(help="Validate versioned reviewer corpora.", no_args_is_help=True)
app.add_typer(cluster_app, name="cluster")
app.add_typer(tasks_app, name="tasks")
app.add_typer(corpus_app, name="corpus")
console = Console()


@cluster_app.command("up")
def cluster_up() -> None:
    """Create the k3d cluster and evaluation namespace."""
    cluster_mod.cluster_up()


@cluster_app.command("down")
def cluster_down() -> None:
    """Delete the k3d cluster."""
    cluster_mod.cluster_down()


@cluster_app.command("status")
def cluster_status() -> None:
    """Show cluster nodes and eval pods."""
    cluster_mod.cluster_status()


@tasks_app.command("list")
def tasks_list() -> None:
    """List available tasks."""
    for task in list_tasks():
        console.print(f"[bold]{task.id}[/bold]  ({task.language}, "
                      f"tags: {', '.join(task.tags) or '-'})")


@tasks_app.command("validate")
def tasks_validate(task_id: str) -> None:
    """Require the starter to fail and the oracle solution to pass."""
    task = load_task(task_id)
    cluster_mod.ensure_cluster()
    record = validate_task(task)
    c = record.correctness
    if c.resolved:
        console.print(f"[green]task {task_id} valid[/green]: oracle passes "
                      f"{c.passed}/{c.total} hidden tests")
    else:
        console.print(f"[red]task {task_id} INVALID[/red]: {c.passed}/{c.total} passed, "
                      f"failures: {c.failures or c.infra_error}")
        console.print(f"see {record.run_dir}/eval-output.txt")
        raise typer.Exit(1)


@corpus_app.command("validate")
def corpus_validate(
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    execute: bool = typer.Option(
        True, "--execute/--no-execute", help="Run base/head reproducers."
    ),
) -> None:
    """Validate corpus hashes, gold diff locations, and reproducers."""
    from .corpus import validate_corpus

    try:
        result = validate_corpus(manifest, execute=execute)
    except (OSError, ValueError) as exc:
        console.print(f"[red]could not validate corpus: {exc}[/red]")
        raise typer.Exit(1) from None
    if result.valid:
        console.print(
            f"[green]corpus valid[/green]: {result.corpus_id} v{result.version}, "
            f"{len(result.reproducers)} reproducer(s) checked"
        )
        return
    for error in result.errors:
        console.print(f"[red]{error}[/red]")
    raise typer.Exit(2)


@app.command()
def review(
    repo: Path = typer.Option(Path("."), "--repo", exists=True, file_okay=False,
                              help="Git repository to review."),
    base: str = typer.Option(None, "--base",
                             help="Base ref (default: origin/HEAD, main, or master)."),
    head: str = typer.Option(None, "--head",
                             help="Head ref; omit to review the working tree."),
    test_cmd: str = typer.Option(None, "--test-cmd",
                                 help="Test command run on head (classical grader) and "
                                      "replayed against base (reverse-classical)."),
    check: list[str] = typer.Option(None, "--check",
                                    help="Command grader: shell command that must exit 0 "
                                         "(repeatable, e.g. --check 'ruff check .')."),
    gen_tests: bool = typer.Option(False, "--gen-tests",
                                   help="LLM-generated discriminating test: must pass on "
                                        "head and fail on base. Requires explicit local "
                                        "execution trust."),
    allow_local_execution: bool = typer.Option(
        False,
        "--allow-local-execution",
        help="Allow test/check/generated code from the change to run on this Mac.",
    ),
    context: str = typer.Option(None, "--context",
                                help="Ticket/spec text the change should implement; "
                                     "prefix with @ to read from a file."),
    policy: Path = typer.Option(
        None,
        "--policy",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Review policy file (default: <repo>/.agent-eval.yaml).",
    ),
    scan: bool = typer.Option(True, help="Run scanners over the changed files."),
    llm: bool = typer.Option(True, help="Run the LLM risk review."),
    out: Path = typer.Option(None, "--out",
                             help="Report directory (default: <repo>/.agent-eval/reviews/<ts>)."),
) -> None:
    """Pre-merge change report: scope/command/test graders (frontier-eval
    style), scanners, risk signals, and a verified-findings LLM review.
    No cluster needed."""
    from .review import print_review, review_change

    if context and context.startswith("@"):
        context = Path(context[1:]).read_text()
    try:
        report = review_change(repo, base, head, test_cmd=test_cmd,
                               context=context, checks=list(check or []),
                               gen_tests=gen_tests, policy_path=policy,
                               allow_local_execution=allow_local_execution,
                               run_scans=scan, run_llm=llm, out_dir=out)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    print_review(report)
    if report.risk == "high" or report.blocked:
        raise typer.Exit(2)  # CI-friendly: high risk / blocked fails the check


def _metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


@app.command("benchmark-review")
def benchmark_review(
    manifest: Path = typer.Option(
        ..., "--manifest", exists=True, dir_okay=False,
        help="Gold-labeled review benchmark YAML manifest.",
    ),
    reviews: Path = typer.Option(
        ..., "--reviews", file_okay=False,
        help="Directory containing <case-id>.json reviewer outputs.",
    ),
    out: Path = typer.Option(
        None, "--out", dir_okay=False,
        help="Write item-level benchmark results as JSON.",
    ),
    min_precision: float = typer.Option(
        None, "--min-precision", min=0.0, max=1.0,
        help="Fail if aggregate precision is below this value.",
    ),
    min_recall: float = typer.Option(
        None, "--min-recall", min=0.0, max=1.0,
        help="Fail if aggregate recall is below this value.",
    ),
    min_critical_recall: float = typer.Option(
        None, "--min-critical-recall", min=0.0, max=1.0,
        help="Fail if blocker/major recall is below this value.",
    ),
    max_fp_per_case: float = typer.Option(
        None, "--max-fp-per-case", min=0.0,
        help="Fail if average false positives per case exceeds this value.",
    ),
    fail_on_missing: bool = typer.Option(
        True, "--fail-on-missing/--allow-missing",
        help="Fail the regression gate when a case has no complete reviewer output.",
    ),
) -> None:
    """Score reviewer outputs against deterministic, gold-labeled findings."""
    import yaml

    from .review_benchmark import load_manifest, score_benchmark

    for option, threshold in (
        ("--min-precision", min_precision),
        ("--min-recall", min_recall),
        ("--min-critical-recall", min_critical_recall),
        ("--max-fp-per-case", max_fp_per_case),
    ):
        if threshold is not None and not math.isfinite(threshold):
            raise typer.BadParameter("must be finite", param_hint=option)

    try:
        result = score_benchmark(load_manifest(manifest), reviews)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        console.print(f"[red]could not score benchmark: {exc}[/red]")
        raise typer.Exit(1) from None

    metrics = result.metrics
    table = Table(title="AI reviewer benchmark", show_edge=False)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("cases", str(metrics.case_count))
    table.add_row("complete cases", str(metrics.scored_case_count))
    table.add_row("TP / FP / FN", f"{metrics.tp} / {metrics.fp} / {metrics.fn}")
    table.add_row("precision", _metric(metrics.precision))
    table.add_row("recall", _metric(metrics.recall))
    table.add_row("F1", _metric(metrics.f1))
    table.add_row("blocker + major recall", _metric(metrics.blocker_major_recall))
    table.add_row("severity accuracy", _metric(metrics.severity_accuracy))
    table.add_row("false positives / case", _metric(metrics.false_positives_per_case))
    table.add_row("false positives / KLoC", _metric(metrics.false_positives_per_kloc))
    table.add_row("clean-case accuracy", _metric(metrics.clean_case_accuracy))
    console.print(table)

    unavailable = [
        case.case_id for case in result.cases
        if case.status != "scored"
    ]
    if unavailable:
        console.print(
            f"[yellow]{len(unavailable)} missing or incomplete reviewer "
            "output(s) were scored as zero findings[/yellow]"
        )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        console.print(f"benchmark result: {out}")

    gates = [
        ("precision", metrics.precision, min_precision, "minimum"),
        ("recall", metrics.recall, min_recall, "minimum"),
        (
            "blocker + major recall",
            metrics.blocker_major_recall,
            min_critical_recall,
            "minimum",
        ),
        (
            "false positives / case",
            metrics.false_positives_per_case,
            max_fp_per_case,
            "maximum",
        ),
    ]
    failures = []
    if unavailable and fail_on_missing:
        failures.append(
            f"{len(unavailable)} benchmark case(s) have no reviewer output "
            "or an incomplete findings payload"
        )
    for name, value, threshold, direction in gates:
        if threshold is None:
            continue
        failed = value is None or (
            value < threshold if direction == "minimum" else value > threshold
        )
        if failed:
            failures.append(
                f"{name} {_metric(value)} does not meet "
                f"{direction} {threshold:.3f}"
            )
    if failures:
        for failure in failures:
            console.print(f"[red]gate failed: {failure}[/red]")
        raise typer.Exit(2)


@app.command("benchmark-experiment")
def benchmark_experiment(
    experiment: Path = typer.Option(
        ..., "--experiment", exists=True, dir_okay=False,
        help="Versioned repeated reviewer experiment YAML.",
    ),
    out: Path = typer.Option(None, "--out", dir_okay=False),
    require_budgets: bool = typer.Option(
        True,
        "--require-budgets/--allow-budget-failures",
        help="Exit 2 when any system is incomplete or exceeds a declared budget.",
    ),
) -> None:
    """Compare repeated single-reviewer and quorum-panel outputs."""
    from .review_experiment import run_experiment

    try:
        result = run_experiment(experiment)
    except (OSError, ValueError) as exc:
        console.print(f"[red]could not run reviewer experiment: {exc}[/red]")
        raise typer.Exit(1) from None
    table = Table(title="Reviewer experiment", show_edge=False)
    for column in ("system", "mode", "trials", "F1", "FP/case", "latency", "tokens", "cost", "stable", "budget"):
        table.add_column(column)
    for system in result.systems:
        stats = system.statistics
        table.add_row(
            system.system_id,
            system.mode,
            str(len(system.trials)),
            _metric(stats.f1.mean),
            _metric(stats.false_positives_per_case.mean),
            _metric(stats.latency_s.mean),
            _metric(stats.tokens.mean),
            _metric(stats.cost_usd.mean),
            _metric(system.finding_stability.mean_jaccard),
            "yes" if system.budget.eligible else "no",
        )
    console.print(table)
    for comparison in result.paired_comparisons:
        console.print(
            f"paired {comparison.candidate_system_id} vs "
            f"{comparison.baseline_system_id}: "
            f"{comparison.wins} win / {comparison.ties} tie / "
            f"{comparison.losses} loss, {comparison.compared_pairs}/"
            f"{comparison.expected_pairs} comparable"
        )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        console.print(f"experiment result: {out}")
    ineligible = [system for system in result.systems if not system.budget.eligible]
    if require_budgets and ineligible:
        for system in ineligible:
            console.print(
                f"[red]budget failed for {system.system_id}: "
                f"{'; '.join(system.budget.failures)}[/red]"
            )
        raise typer.Exit(2)


@app.command()
def doctor() -> None:
    """Check local prerequisites and show which features each one unlocks."""
    import os
    import shutil as sh

    checks = [
        ("git", sh.which("git") is not None, "review + diffing", "xcode-select --install"),
        ("uv/uvx", sh.which("uvx") is not None, "ruff + semgrep scanners",
         "brew install uv"),
        ("codex CLI", sh.which("codex") is not None, "codex agent + LLM review/judge",
         "npm i -g @openai/codex && codex login"),
        ("codex login", (Path.home() / ".codex" / "auth.json").is_file(),
         "codex auth inside sandbox pods", "codex login"),
        ("ANTHROPIC_API_KEY", bool(os.environ.get("ANTHROPIC_API_KEY")),
         "reusable claude fallback + claude judge", "export ANTHROPIC_API_KEY=..."),
        ("credential broker", bool(os.environ.get("AGENT_EVAL_CREDENTIAL_COMMAND")),
         "provider-minted per-trial credentials (recommended)",
         "export AGENT_EVAL_CREDENTIAL_COMMAND='broker-command'"),
        ("docker", sh.which("docker") is not None, "agent benchmark mode (k3s)",
         "brew install colima docker && colima start"),
        ("kubectl", sh.which("kubectl") is not None, "agent benchmark mode (k3s)",
         "brew install kubectl"),
        ("k3d", sh.which("k3d") is not None, "agent benchmark mode (k3s)",
         "brew install k3d"),
        ("gitleaks", sh.which("gitleaks") is not None,
         "secret gate before external diff review/judging",
         "brew install gitleaks"),
        ("trivy", sh.which("trivy") is not None, "dependency vuln scanning (optional)",
         "brew install trivy"),
    ]
    table = Table(title="agent-eval doctor")
    for col in ("check", "status", "unlocks", "fix"):
        table.add_column(col)
    for name, ok, unlocks, fix in checks:
        table.add_row(name, "[green]ok[/green]" if ok else "[red]missing[/red]",
                      unlocks, "" if ok else fix)
    console.print(table)
    console.print(
        "\nDeterministic `agent-eval review` needs git. Scanner-backed external "
        "LLM review also needs uvx, gitleaks, and an authenticated LLM backend. "
        "`agent-eval run` needs docker/kubectl/k3d."
    )


@app.command()
def evaluate(
    task_id: str = typer.Option(..., "--task"),
    workspace: Path = typer.Option(..., "--workspace", exists=True, file_okay=False),
    scan: bool = typer.Option(True, help="Run static/security scanners."),
    judge: bool = typer.Option(True, help="Run the LLM judge."),
    gate: bool = typer.Option(
        False, "--gate", help="Exit 2 unless the task acceptance policy accepts."
    ),
) -> None:
    """Evaluate an already-produced workspace (eval-only mode)."""
    task = load_task(task_id)
    cluster_mod.ensure_cluster()
    record = evaluate_workspace(task, workspace.resolve(),
                                run_scans=scan, run_judge=judge)
    print_run_detail(record.run_id)
    print_runs_table(task_id, limit=5)
    if gate and (record.outcome is None or not record.outcome.accepted):
        raise typer.Exit(2)


@app.command()
def run(
    task_id: str = typer.Option(..., "--task"),
    agent: str = typer.Option("claude-code", "--agent"),
    trials: int = typer.Option(1, "--trials", min=1),
    model: str = typer.Option(None, "--model", help="Override the agent's model."),
    rebuild: bool = typer.Option(False, help="Force rebuild of the task image."),
    scan: bool = typer.Option(True, help="Run static/security scanners."),
    judge: bool = typer.Option(True, help="Run the LLM judge."),
    experiment_id: str = typer.Option(
        None, "--experiment-id", help="Pair trials across agents in comparisons."
    ),
    gate: bool = typer.Option(
        False, "--gate", help="Exit 2 if any trial is not accepted."
    ),
) -> None:
    """Full harness: launch the coding agent in k3s, then evaluate its output."""
    from .agents import get_adapter
    from .runner import ensure_image, run_agent_trial

    task = load_task(task_id)
    adapter = get_adapter(agent)
    cluster_mod.ensure_cluster()
    ensure_image(task, rebuild=rebuild)
    records = []
    for trial in range(1, trials + 1):
        console.rule(f"trial {trial}/{trials}")
        record = run_agent_trial(task, adapter, trial=trial, model=model,
                                 run_scans=scan, run_judge=judge,
                                 experiment_id=experiment_id)
        records.append(record)
        status = "resolved" if record.correctness.resolved else "not resolved"
        console.print(f"trial {trial}: [bold]{status}[/bold] "
                      f"({record.correctness.passed}/{record.correctness.total} tests)")
    print_runs_table(task_id, limit=trials + 5)
    print_trial_summary(records)
    if gate and any(record.outcome is None or not record.outcome.accepted
                    for record in records):
        raise typer.Exit(2)


@app.command("compare")
def compare(
    task_id: str = typer.Option(..., "--task"),
    out: Path = typer.Option(None, "--out", dir_okay=False),
    limit: int = typer.Option(1000, "--limit", min=1),
    include_controls: bool = typer.Option(
        False,
        "--include-controls",
        help="Include oracle and external eval-only records.",
    ),
) -> None:
    """Compare persisted coding-agent outcomes by agent and model."""
    from .agent_comparison import compare_agents
    from .metrics import RunRecord, load_runs

    records = [
        RunRecord.model_validate_json(row["results_json"])
        for row in load_runs(task_id, limit)
    ]
    if not include_controls:
        records = [
            record for record in records
            if record.agent not in {"external", "oracle"}
        ]
    if not records:
        console.print("[yellow]no runs recorded yet[/yellow]")
        return
    result = compare_agents(records)
    table = Table(title="Coding-agent comparison", show_edge=False)
    for column in (
        "agent/model", "n", "resolved", "accepted", "95% CI", "infra",
        "time p50/p95", "tokens p50", "cost p50", "judge p50",
    ):
        table.add_column(column)
    for summary in result.summaries:
        interval = summary.resolved_wilson_95
        table.add_row(
            f"{summary.agent}/{summary.model}",
            str(summary.sample_size),
            (
                f"{summary.resolved}/{summary.correctness_evidence_count} "
                f"({summary.resolved_rate:.1%})"
                if summary.resolved_rate is not None
                else "n/a (legacy evidence incomplete)"
            ),
            (
                f"{summary.accepted}/{summary.accepted_evidence_count} "
                f"({summary.accepted_rate:.1%})"
                if summary.accepted_rate is not None
                else "n/a"
            ),
            (
                f"{interval.lower:.1%}..{interval.upper:.1%}"
                if interval else "n/a"
            ),
            f"{summary.infrastructure_failure_rate:.1%}",
            f"{_metric(summary.wall_time_s.median)}/{_metric(summary.wall_time_s.p95)}",
            _metric(summary.total_tokens.median),
            _metric(summary.cost_usd.median),
            _metric(summary.judge_score.median),
        )
    console.print(table)
    if not result.paired:
        console.print(
            "[yellow]no paired comparison: use the same --experiment-id and "
            "trial numbers for each agent[/yellow]"
        )
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        console.print(f"comparison result: {out}")


@app.command("verify-run")
def verify_run(
    run_id: str = typer.Option(..., "--run"),
) -> None:
    """Recompute a run's unsigned local provenance and artifact hashes."""
    from .attestation import verify_attestation
    from .metrics import load_run

    record = load_run(run_id)
    if record is None:
        console.print(f"[red]run {run_id} not found[/red]")
        raise typer.Exit(1)
    statement = record.run_dir / "attestation.json"
    if not statement.is_file():
        console.print(f"[red]run {run_id} has no attestation[/red]")
        raise typer.Exit(2)
    result = verify_attestation(
        statement,
        artifact_root=record.run_dir,
        task_root=load_task(record.task_id).path,
        harness_repo=Path(__file__).resolve().parents[2],
    )
    if result.ok:
        console.print(
            f"[green]verified[/green]: {result.subjects_checked} artifact(s), "
            "task tree, and harness Git state match the unsigned statement"
        )
        return
    for failure in result.failures:
        console.print(f"[red]{failure.code}: {failure.message}[/red]")
    raise typer.Exit(2)


@app.command()
def report(
    task_id: str = typer.Option(None, "--task"),
    run_id: str = typer.Option(None, "--run"),
    markdown: Path = typer.Option(None, "--markdown", help="Write a markdown report here."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Show recorded runs, one run's full results, or export markdown."""
    if run_id:
        print_run_detail(run_id)
        return
    if markdown:
        markdown.write_text(markdown_report(task_id, limit))
        console.print(f"wrote {markdown}")
        return
    print_runs_table(task_id, limit)


if __name__ == "__main__":
    app()
