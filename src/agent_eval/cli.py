"""agent-eval CLI: cluster lifecycle, task management, runs, and reports."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from . import cluster as cluster_mod
from .report import markdown_report, print_run_detail, print_runs_table, print_trial_summary
from .runner import evaluate_workspace, validate_task
from .task import list_tasks, load_task

app = typer.Typer(help="Coding-agent evaluation harness on k3s.", no_args_is_help=True)
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
def evaluate(
    task_id: str = typer.Option(..., "--task"),
    workspace: Path = typer.Option(..., "--workspace", exists=True, file_okay=False),
    scan: bool = typer.Option(True, help="Run static/security scanners."),
    judge: bool = typer.Option(True, help="Run the LLM judge."),
) -> None:
    """Evaluate an already-produced workspace (eval-only mode)."""
    task = load_task(task_id)
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
