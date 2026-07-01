"""Thin kubectl wrapper. All cluster interaction shells out to kubectl; file
transfer uses tar pipes (more reliable than kubectl cp for directories)."""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

NAMESPACE = "agent-eval"
KUBE_CONTEXT = "k3d-agent-eval"


class KubeError(RuntimeError):
    pass


def kubectl(*args: str, input: bytes | None = None, timeout: int | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE, *args]
    proc = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise KubeError(
            f"kubectl {' '.join(args[:4])}... failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[-2000:]}"
        )
    return proc


def ensure_namespace() -> None:
    proc = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "create", "namespace", NAMESPACE],
        capture_output=True,
    )
    if proc.returncode != 0 and b"AlreadyExists" not in proc.stderr:
        raise KubeError(proc.stderr.decode(errors="replace"))


@dataclass
class Pod:
    name: str

    def wait_ready(self, timeout: int = 300) -> None:
        kubectl("wait", "--for=condition=Ready", f"pod/{self.name}",
                f"--timeout={timeout}s", timeout=timeout + 30)

    def copy_dir_to(self, local_dir: Path, remote_dir: str) -> None:
        """Copy the *contents* of local_dir into remote_dir (created if absent)."""
        self.exec(f"mkdir -p {remote_dir}", timeout=60)
        tar = subprocess.run(
            ["tar", "-C", str(local_dir), "-cf", "-", "."],
            capture_output=True, check=True,
        )
        kubectl("exec", "-i", self.name, "--", "tar", "-C", remote_dir, "-xf", "-",
                input=tar.stdout, timeout=300)

    def copy_dir_from(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        proc = kubectl("exec", self.name, "--", "tar", "-C", remote_dir, "-cf", "-", ".",
                       timeout=300)
        subprocess.run(["tar", "-C", str(local_dir), "-xf", "-"],
                       input=proc.stdout, capture_output=True, check=True)

    def exec(self, command: str, timeout: int | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run a shell command in the pod. Returns the completed process; the
        returncode is the remote command's exit code."""
        prefix = ""
        if env:
            prefix = " ".join(f"{k}={v}" for k, v in env.items()) + " "
        return kubectl("exec", self.name, "--", "sh", "-c", prefix + command,
                       timeout=timeout, check=False)

    def delete(self) -> None:
        kubectl("delete", "pod", self.name, "--ignore-not-found", "--wait=false",
                check=False, timeout=60)


def create_sandbox_pod(prefix: str, image: str, *, env_from_secret: str | None = None,
                       active_deadline: int = 3600) -> Pod:
    """Create a sleeping pod we can copy files into and exec commands in."""
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    spec: dict = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"app": "agent-eval", "phase": prefix}},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": active_deadline,
            "containers": [{
                "name": "sandbox",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep infinity"],
                "workingDir": "/workspace",
            }],
        },
    }
    if env_from_secret:
        # optional: adapters with file-based auth (codex) run without the secret
        spec["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": env_from_secret, "optional": True}}]
    kubectl("apply", "-f", "-", input=json.dumps(spec).encode(), timeout=60)
    return Pod(name)
