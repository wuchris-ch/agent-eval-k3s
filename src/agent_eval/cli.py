"""agent-eval CLI: cluster lifecycle, task management, runs, and reports."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

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
app.add_typer(cluster_app, name="cluster")
app.add_typer(tasks_app, name="tasks")
console = Console()


@cluster_app.command("up")
def cluster_up() -> None:
    """Create the k3d cluster, namespace, and API-key secret."""
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
    """Run the oracle solution through the eval pipeline; it must pass."""
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
                                        "head and fail on base. Runs generated code "
                                        "locally; use only on changes you trust."),
    context: str = typer.Option(None, "--context",
                                help="Ticket/spec text the change should implement; "
                                     "prefix with @ to read from a file."),
    policy: Path = typer.Option(None, "--policy",
                                help="Review policy file (default: <repo>/.agent-eval.yaml)."),
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
                               run_scans=scan, run_llm=llm, out_dir=out)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    print_review(report)
    if report.risk == "high" or report.blocked:
        raise typer.Exit(2)  # CI-friendly: high risk / blocked fails the check


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
         "claude-code agent + claude judge", "export ANTHROPIC_API_KEY=..."),
        ("docker", sh.which("docker") is not None, "agent benchmark mode (k3s)",
         "brew install colima docker && colima start"),
        ("kubectl", sh.which("kubectl") is not None, "agent benchmark mode (k3s)",
         "brew install kubectl"),
        ("k3d", sh.which("k3d") is not None, "agent benchmark mode (k3s)",
         "brew install k3d"),
        ("gitleaks", sh.which("gitleaks") is not None, "secret scanning (optional)",
         "brew install gitleaks"),
        ("trivy", sh.which("trivy") is not None, "dependency vuln scanning (optional)",
         "brew install trivy"),
    ]
    from rich.table import Table
    table = Table(title="agent-eval doctor")
    for col in ("check", "status", "unlocks", "fix"):
        table.add_column(col)
    for name, ok, unlocks, fix in checks:
        table.add_row(name, "[green]ok[/green]" if ok else "[red]missing[/red]",
                      unlocks, "" if ok else fix)
    console.print(table)
    console.print("\n`agent-eval review` needs only git (+ uvx and an LLM backend "
                  "for full reports). `agent-eval run` needs docker/kubectl/k3d.")


@app.command()
def evaluate(
    task_id: str = typer.Option(..., "--task"),
    workspace: Path = typer.Option(..., "--workspace", exists=True, file_okay=False),
    scan: bool = typer.Option(True, help="Run static/security scanners."),
    judge: bool = typer.Option(True, help="Run the LLM judge."),
) -> None:
    """Evaluate an already-produced workspace (eval-only mode)."""
    task = load_task(task_id)
    cluster_mod.ensure_cluster()
    record = evaluate_workspace(task, workspace.resolve(),
                                run_scans=scan, run_judge=judge)
    print_run_detail(record.run_id)
    print_runs_table(task_id, limit=5)


@app.command()
def run(
    task_id: str = typer.Option(..., "--task"),
    agent: str = typer.Option("claude-code", "--agent"),
    trials: int = typer.Option(1, "--trials", min=1),
    model: str = typer.Option(None, "--model", help="Override the agent's model."),
    rebuild: bool = typer.Option(False, help="Force rebuild of the task image."),
    scan: bool = typer.Option(True, help="Run static/security scanners."),
    judge: bool = typer.Option(True, help="Run the LLM judge."),
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
                                 run_scans=scan, run_judge=judge)
        records.append(record)
        status = "resolved" if record.correctness.resolved else "not resolved"
        console.print(f"trial {trial}: [bold]{status}[/bold] "
                      f"({record.correctness.passed}/{record.correctness.total} tests)")
    print_runs_table(task_id, limit=trials + 5)
    print_trial_summary(records)


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
