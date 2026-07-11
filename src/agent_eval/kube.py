"""Thin kubectl wrapper. All cluster interaction shells out to kubectl; file
transfer uses tar pipes (more reliable than kubectl cp for directories)."""

from __future__ import annotations

import json
import subprocess
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

NAMESPACE = "agent-eval"
KUBE_CONTEXT = "k3d-agent-eval"

# Conservative defaults for arbitrary-code workloads. Task images still run as
# their declared user and retain a writable filesystem because existing agent
# CLIs and test suites require both; the controls below remove ambient cluster
# identity and Linux privilege without changing that task contract.
DEFAULT_SANDBOX_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "128Mi", "ephemeral-storage": "256Mi"},
    "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "4Gi"},
}


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


def sandbox_pod_manifest(name: str, prefix: str, image: str, *,
                         env_from_secret: str | None = None,
                         active_deadline: int = 3600) -> dict:
    """Return the auditable pod manifest used for agent and eval sandboxes.

    The service-account token and service discovery environment are disabled,
    while the container gets seccomp, no privilege escalation, no Linux
    capabilities, and bounded resources. Network egress remains available
    because coding agents must reach their model provider.
    """
    spec: dict = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {"app": "agent-eval", "phase": prefix}},
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "restartPolicy": "Never",
            "activeDeadlineSeconds": active_deadline,
            "terminationGracePeriodSeconds": 1,
            "containers": [{
                "name": "sandbox",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep infinity"],
                "workingDir": "/workspace",
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "resources": deepcopy(DEFAULT_SANDBOX_RESOURCES),
            }],
        },
    }
    if env_from_secret:
        # optional: adapters with file-based auth (codex) run without the secret
        spec["spec"]["containers"][0]["envFrom"] = [
            {"secretRef": {"name": env_from_secret, "optional": True}}]
    return spec


def create_sandbox_pod(prefix: str, image: str, *, env_from_secret: str | None = None,
                       active_deadline: int = 3600) -> Pod:
    """Create a sleeping pod we can copy files into and exec commands in."""
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    spec = sandbox_pod_manifest(
        name, prefix, image, env_from_secret=env_from_secret,
        active_deadline=active_deadline,
    )
    kubectl("apply", "-f", "-", input=json.dumps(spec).encode(), timeout=60)
    return Pod(name)
