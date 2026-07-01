"""k3d cluster lifecycle: create/delete the k3s-in-docker cluster, import task
images, and manage the API-key secret."""

from __future__ import annotations

import os
import subprocess

from rich.console import Console

from .kube import KUBE_CONTEXT, NAMESPACE, KubeError, ensure_namespace, kubectl

CLUSTER_NAME = "agent-eval"
SECRET_NAME = "agent-api-keys"
console = Console()


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise KubeError(f"{' '.join(cmd[:3])} failed: {proc.stderr[-2000:]}")
    return proc


def cluster_exists() -> bool:
    proc = subprocess.run(["k3d", "cluster", "list", "-o", "json"],
                          capture_output=True, text=True)
    return proc.returncode == 0 and f'"name": "{CLUSTER_NAME}"' in proc.stdout.replace("'", '"')


def cluster_up() -> None:
    if cluster_exists():
        console.print(f"[yellow]cluster {CLUSTER_NAME} already exists[/yellow]")
    else:
        console.print(f"creating k3d cluster [bold]{CLUSTER_NAME}[/bold]...")
        _run(["k3d", "cluster", "create", CLUSTER_NAME, "--agents", "1", "--wait"])
    ensure_namespace()
    sync_api_key_secret()
    console.print("[green]cluster ready[/green]")


def cluster_down() -> None:
    _run(["k3d", "cluster", "delete", CLUSTER_NAME])
    console.print(f"[green]cluster {CLUSTER_NAME} deleted[/green]")


def cluster_status() -> None:
    if not cluster_exists():
        console.print(f"[red]cluster {CLUSTER_NAME} does not exist[/red] "
                      "(run: agent-eval cluster up)")
        return
    proc = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "get", "nodes", "-o", "wide"],
        capture_output=True, text=True,
    )
    console.print(proc.stdout or proc.stderr)
    pods = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, "get", "pods"],
        capture_output=True, text=True,
    )
    console.print(pods.stdout or pods.stderr)


def sync_api_key_secret() -> None:
    """Create/update the secret holding ANTHROPIC_API_KEY from the host env."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        console.print("[yellow]ANTHROPIC_API_KEY not set; agent phase will not work "
                      "until you export it and re-run `agent-eval cluster up`[/yellow]")
        return
    manifest = kubectl("create", "secret", "generic", SECRET_NAME,
                       f"--from-literal=ANTHROPIC_API_KEY={key}",
                       "--dry-run=client", "-o", "json", timeout=30)
    kubectl("apply", "-f", "-", input=manifest.stdout, timeout=30)
    console.print(f"secret [bold]{SECRET_NAME}[/bold] synced")


def build_and_import_image(context_dir: str, tag: str) -> None:
    console.print(f"building image [bold]{tag}[/bold]...")
    _run(["docker", "build", "-t", tag, context_dir], timeout=1800)
    console.print("importing image into cluster...")
    _run(["k3d", "image", "import", tag, "-c", CLUSTER_NAME], timeout=600)
