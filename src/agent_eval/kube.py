"""Thin kubectl wrapper. All cluster interaction shells out to kubectl; file
transfer uses tar pipes (more reliable than kubectl cp for directories)."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from copy import deepcopy
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path

from .task import DEFAULT_SANDBOX_RESOURCES

NAMESPACE = "agent-eval"
KUBE_CONTEXT = "k3d-agent-eval"
CREDENTIAL_MOUNT = "/var/run/agent-eval-credentials"
PROXY_PORT = 3128
PROXY_ACTIVE_DEADLINE_SECONDS = 3600
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_MEMBERS = 50_000
MAX_SNAPSHOT_PATH_BYTES = 4_096
MAX_ARCHIVE_STDERR_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_SECURITY = {
    "run_as_non_root": True,
    "run_as_user": 10001,
    "run_as_group": 10001,
    "read_only_root_filesystem": True,
}


class KubeError(RuntimeError):
    pass


class UnsafeArchiveError(KubeError):
    """Raised when an untrusted pod snapshot is unsafe to extract."""


class CommandOutputLimitError(KubeError):
    """Raised after a pod command exceeds the bounded host output capture."""

    def __init__(self, stdout: bytes, stderr: bytes):
        super().__init__(
            f"pod command output exceeded {MAX_COMMAND_OUTPUT_BYTES} bytes"
        )
        self.stdout = stdout
        self.stderr = stderr


def _bounded_output(streams: tuple, maximum: int) -> tuple[bytes, bytes]:
    remaining = maximum
    captured = []
    for stream in streams:
        stream.seek(0)
        data = stream.read(remaining)
        captured.append(data)
        remaining -= len(data)
    return captured[0], captured[1]


def _run_bounded_command(
    command: list[str], timeout: int | None, *, stdin=None
) -> subprocess.CompletedProcess:
    """Run a host command with disk-backed, size-bounded stdout and stderr."""

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            command, stdin=stdin, stdout=stdout, stderr=stderr
        )
        deadline = time.monotonic() + timeout if timeout is not None else None
        while process.poll() is None:
            size = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
            if size > MAX_COMMAND_OUTPUT_BYTES:
                process.kill()
                process.wait()
                captured = _bounded_output(
                    (stdout, stderr), MAX_COMMAND_OUTPUT_BYTES
                )
                raise CommandOutputLimitError(*captured)
            if deadline is not None and time.monotonic() >= deadline:
                process.kill()
                process.wait()
                captured = _bounded_output(
                    (stdout, stderr), MAX_COMMAND_OUTPUT_BYTES
                )
                raise subprocess.TimeoutExpired(
                    command, timeout, output=captured[0], stderr=captured[1]
                )
            time.sleep(0.05)
        size = os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size
        captured = _bounded_output((stdout, stderr), MAX_COMMAND_OUTPUT_BYTES)
        if size > MAX_COMMAND_OUTPUT_BYTES:
            raise CommandOutputLimitError(*captured)
        return subprocess.CompletedProcess(
            command, process.returncode, stdout=captured[0], stderr=captured[1]
        )


def _validate_local_transfer_tree(local_dir: Path) -> list[str]:
    """Validate and size a local tree before archiving it for a pod."""

    try:
        root = local_dir.resolve(strict=True)
    except OSError as exc:
        raise UnsafeArchiveError(
            f"local transfer tree is unavailable: {type(exc).__name__}"
        ) from exc
    if not root.is_dir():
        raise UnsafeArchiveError("local transfer tree is not a directory")

    member_count = 0
    expanded_bytes = 0
    top_entries: list[str] = []
    pending = [(root, Path())]
    while pending:
        directory, relative_dir = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError as exc:
            raise UnsafeArchiveError(
                "local transfer tree contains an unreadable directory"
            ) from exc
        with entries:
            iterator = iter(entries)
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError as exc:
                    raise UnsafeArchiveError(
                        "local transfer tree contains an unreadable directory"
                    ) from exc
                relative = relative_dir / entry.name
                member_count += 1
                if member_count > MAX_SNAPSHOT_MEMBERS:
                    raise UnsafeArchiveError(
                        "local transfer tree contains more than "
                        f"{MAX_SNAPSHOT_MEMBERS} members"
                    )
                if len(os.fsencode(relative.as_posix())) > MAX_SNAPSHOT_PATH_BYTES:
                    raise UnsafeArchiveError(
                        "local transfer tree contains an overlong path"
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise UnsafeArchiveError(
                        f"local transfer path {relative} is unreadable"
                    ) from exc
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append((Path(entry.path), relative))
                elif stat.S_ISREG(metadata.st_mode):
                    expanded_bytes += metadata.st_size
                    if expanded_bytes > MAX_SNAPSHOT_BYTES:
                        raise UnsafeArchiveError(
                            "local transfer tree expands beyond "
                            f"{MAX_SNAPSHOT_BYTES} bytes"
                        )
                else:
                    raise UnsafeArchiveError(
                        f"local transfer path {relative} is not a regular file "
                        "or directory"
                    )
                if not relative_dir.parts:
                    top_entries.append(f"./{entry.name}")
    return sorted(top_entries, key=os.fsencode)


def _local_archive_stream(local_dir: Path):
    """Create a disk-backed local tar with strict tree and stream caps."""

    entries = _validate_local_transfer_tree(local_dir)
    if not entries:
        return None
    archive = tempfile.TemporaryFile()
    stderr = tempfile.TemporaryFile()
    command = [
        "tar", "--no-xattrs", "-C", str(local_dir), "-cf", "-", *entries
    ]
    process = subprocess.Popen(
        command,
        stdout=archive,
        stderr=stderr,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    )
    deadline = time.monotonic() + 300
    try:
        while process.poll() is None:
            if os.fstat(archive.fileno()).st_size > MAX_SNAPSHOT_BYTES:
                process.kill()
                process.wait()
                raise UnsafeArchiveError(
                    f"local transfer archive exceeds {MAX_SNAPSHOT_BYTES} bytes"
                )
            if os.fstat(stderr.fileno()).st_size > MAX_ARCHIVE_STDERR_BYTES:
                process.kill()
                process.wait()
                raise UnsafeArchiveError(
                    "local transfer tar stderr exceeds "
                    f"{MAX_ARCHIVE_STDERR_BYTES} bytes"
                )
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise UnsafeArchiveError(
                    "local transfer archive timed out after 300s"
                )
            time.sleep(0.05)
        if os.fstat(archive.fileno()).st_size > MAX_SNAPSHOT_BYTES:
            raise UnsafeArchiveError(
                f"local transfer archive exceeds {MAX_SNAPSHOT_BYTES} bytes"
            )
        stderr_size = os.fstat(stderr.fileno()).st_size
        if stderr_size > MAX_ARCHIVE_STDERR_BYTES:
            raise UnsafeArchiveError(
                "local transfer tar stderr exceeds "
                f"{MAX_ARCHIVE_STDERR_BYTES} bytes"
            )
        if process.returncode != 0:
            stderr.seek(max(0, stderr_size - 2000))
            detail = stderr.read(2000).decode(errors="replace")
            raise UnsafeArchiveError(
                f"could not archive local transfer tree: {detail}"
            )
        archive.seek(0)
        return archive
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        archive.close()
        raise
    finally:
        stderr.close()


def _pod_archive_stream(pod_name: str, remote_dir: str):
    """Capture a pod tar stream to disk with a strict host-memory/size cap."""

    archive = tempfile.TemporaryFile()
    stderr = tempfile.TemporaryFile()
    command = [
        "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE,
        "exec", pod_name, "--", "tar", "-C", remote_dir, "-cf", "-", ".",
    ]
    process = subprocess.Popen(command, stdout=archive, stderr=stderr)
    deadline = time.monotonic() + 300
    try:
        while process.poll() is None:
            if os.fstat(archive.fileno()).st_size > MAX_SNAPSHOT_BYTES:
                process.kill()
                process.wait()
                raise UnsafeArchiveError(
                    f"pod snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
                )
            if os.fstat(stderr.fileno()).st_size > MAX_ARCHIVE_STDERR_BYTES:
                process.kill()
                process.wait()
                raise KubeError(
                    "pod snapshot capture stderr exceeds "
                    f"{MAX_ARCHIVE_STDERR_BYTES} bytes"
                )
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise KubeError("pod snapshot capture timed out after 300s")
            time.sleep(0.05)
        returncode = process.returncode
        if os.fstat(stderr.fileno()).st_size > MAX_ARCHIVE_STDERR_BYTES:
            raise KubeError(
                "pod snapshot capture stderr exceeds "
                f"{MAX_ARCHIVE_STDERR_BYTES} bytes"
            )
        if returncode != 0:
            size = os.fstat(stderr.fileno()).st_size
            stderr.seek(max(0, size - 2000))
            detail = stderr.read(2000).decode(errors="replace")
            raise KubeError(
                f"could not capture pod snapshot (exit {returncode}): {detail}"
            )
        if os.fstat(archive.fileno()).st_size > MAX_SNAPSHOT_BYTES:
            raise UnsafeArchiveError(
                f"pod snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
            )
        archive.seek(0)
        return archive
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        archive.close()
        raise
    finally:
        stderr.close()


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
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "sandbox-default-deny"},
        "spec": {
            "podSelector": {"matchLabels": {"role": "sandbox"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }
    kubectl("apply", "-f", "-", input=json.dumps(manifest).encode(), timeout=30)


@dataclass
class Pod:
    name: str
    network_policy_name: str | None = None

    def wait_ready(self, timeout: int = 300) -> None:
        kubectl("wait", "--for=condition=Ready", f"pod/{self.name}",
                f"--timeout={timeout}s", timeout=timeout + 30)

    def copy_dir_to(self, local_dir: Path, remote_dir: str) -> None:
        """Copy the *contents* of local_dir into remote_dir (created if absent)."""
        archive = _local_archive_stream(local_dir)
        if archive is None:
            self.exec(f"mkdir -p {remote_dir}", timeout=60)
            return
        with closing(archive):
            self.exec(f"mkdir -p {remote_dir}", timeout=60)
            command = [
                "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE,
                "exec", "-i", self.name, "--", "tar",
                "--no-same-owner", "--no-same-permissions", "--touch",
                "-C", remote_dir, "-xf", "-",
            ]
            proc = _run_bounded_command(command, 300, stdin=archive)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).decode(
                    errors="replace"
                )[-2000:]
                raise KubeError(
                    "could not copy local tree into pod "
                    f"(exit {proc.returncode}): {detail}"
                )

    def copy_dir_from(self, remote_dir: str, local_dir: Path) -> None:
        local_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{local_dir.name}-extract-", dir=local_dir.parent)
        )
        try:
            with closing(_pod_archive_stream(self.name, remote_dir)) as stream:
                # Stream members instead of calling getmembers(): a tar near
                # the byte cap can otherwise allocate a huge in-memory list of
                # tiny TarInfo objects before any validation occurs.
                with tarfile.open(fileobj=stream, mode="r|*") as archive:
                    seen: set[str] = set()
                    member_count = 0
                    expanded_bytes = 0
                    for member in archive:
                        member_count += 1
                        if member_count > MAX_SNAPSHOT_MEMBERS:
                            raise UnsafeArchiveError(
                                "pod snapshot contains more than "
                                f"{MAX_SNAPSHOT_MEMBERS} members"
                            )
                        if len(os.fsencode(member.name)) > MAX_SNAPSHOT_PATH_BYTES:
                            raise UnsafeArchiveError(
                                "pod snapshot contains an overlong member path"
                            )
                        if member.isfile():
                            expanded_bytes += member.size
                            if expanded_bytes > MAX_SNAPSHOT_BYTES:
                                raise UnsafeArchiveError(
                                    "pod snapshot expands beyond "
                                    f"{MAX_SNAPSHOT_BYTES} bytes"
                                )
                        elif not member.isdir():
                            raise UnsafeArchiveError(
                                "pod snapshot contains an unsafe archive "
                                f"member type for {member.name!r}"
                            )
                        normalized = os.path.normpath(member.name)
                        if normalized in seen:
                            raise UnsafeArchiveError(
                                "pod snapshot contains duplicate path "
                                f"{normalized!r}"
                            )
                        seen.add(normalized)
                        try:
                            tarfile.data_filter(member, temporary)
                        except (tarfile.FilterError, OSError) as exc:
                            raise UnsafeArchiveError(
                                "pod snapshot contains an unsafe archive "
                                f"member: {exc}"
                            ) from exc
                        archive.extract(member, temporary, filter="data")
            if local_dir.is_symlink() or local_dir.is_file():
                local_dir.unlink()
            elif local_dir.exists():
                shutil.rmtree(local_dir)
            os.replace(temporary, local_dir)
        except (tarfile.TarError, OSError) as exc:
            if isinstance(exc, UnsafeArchiveError):
                raise
            raise UnsafeArchiveError("pod snapshot is not a valid safe tar archive") from exc
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def exec(self, command: str, timeout: int | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run a shell command in the pod. Returns the completed process; the
        returncode is the remote command's exit code."""
        prefix = ""
        if env:
            prefix = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in env.items()
            ) + " "
        host_command = [
            "kubectl", "--context", KUBE_CONTEXT, "-n", NAMESPACE,
            "exec", self.name, "--", "sh", "-c", prefix + command,
        ]
        return _run_bounded_command(host_command, timeout)

    def infrastructure_failure(self, command_exit_code: int | None = None) -> str | None:
        try:
            proc = kubectl(
                "get", "pod", self.name, "-o", "json", check=False, timeout=30
            )
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            try:
                status = json.loads(proc.stdout).get("status", {})
            except (json.JSONDecodeError, AttributeError):
                status = {}
            candidates = [(status.get("reason"), status.get("message"))]
            for container in status.get("containerStatuses", []) or []:
                for state_name in ("state", "lastState"):
                    waiting = container.get(state_name, {}).get("waiting", {})
                    candidates.append(
                        (waiting.get("reason"), waiting.get("message"))
                    )
                    terminated = container.get(state_name, {}).get("terminated", {})
                    candidates.append((terminated.get("reason"), terminated.get("message")))
            for condition in status.get("conditions", []) or []:
                candidates.append((condition.get("reason"), condition.get("message")))
            for reason, message in candidates:
                detail = " ".join(part for part in (reason, message) if part)
                lowered = detail.lower()
                if reason in {
                    "OOMKilled",
                    "Evicted",
                    "DeadlineExceeded",
                    "Unschedulable",
                    "ErrImagePull",
                    "ImagePullBackOff",
                    "InvalidImageName",
                    "CreateContainerConfigError",
                    "CreateContainerError",
                    "RunContainerError",
                    "CrashLoopBackOff",
                } or any(
                    marker in lowered
                    for marker in (
                        "insufficient cpu",
                        "insufficient memory",
                        "ephemeral-storage",
                        "memory pressure",
                        "disk pressure",
                    )
                ):
                    return detail[:1000]
        if command_exit_code == 137:
            return "command exited 137 (SIGKILL; resource-limit termination possible)"
        return None

    def image_digest(self) -> str | None:
        """Return the image digest reported by the container runtime."""

        proc = kubectl(
            "get", "pod", self.name, "-o", "json", check=False, timeout=30
        )
        if proc.returncode != 0:
            return None
        try:
            statuses = json.loads(proc.stdout).get("status", {}).get(
                "containerStatuses", []
            )
            image_id = statuses[0].get("imageID", "")
        except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
            return None
        match = re.search(r"sha256:[0-9a-fA-F]{64}", image_id)
        return match.group(0).lower() if match else None

    def delete(self) -> None:
        kubectl(
            "delete", "pod", self.name, "--ignore-not-found", "--wait=true",
            timeout=60,
        )
        if self.network_policy_name:
            kubectl(
                "delete",
                "networkpolicy",
                self.network_policy_name,
                "--ignore-not-found",
                timeout=60,
            )


@dataclass
class TrialSecret:
    name: str

    def delete(self) -> None:
        kubectl(
            "delete", "secret", self.name, "--ignore-not-found",
            timeout=30,
        )


@dataclass
class EgressProxy:
    name: str
    cluster_ip: str

    @property
    def endpoint(self) -> str:
        return f"http://{self.cluster_ip}:{PROXY_PORT}"

    def wait_ready(self, timeout: int = 180) -> None:
        kubectl(
            "wait", "--for=condition=Ready", f"pod/{self.name}",
            f"--timeout={timeout}s", timeout=timeout + 30,
        )

    def logs(self) -> str:
        proc = kubectl("logs", self.name, check=False, timeout=30)
        return (proc.stdout + proc.stderr).decode(errors="replace")

    def delete(self) -> None:
        pod_deleted = False
        failures = []
        for resource in ("service", "pod", "configmap"):
            try:
                kubectl(
                    "delete", resource, self.name, "--ignore-not-found",
                    "--wait=true" if resource == "pod" else "--wait=false",
                    timeout=60,
                )
                if resource == "pod":
                    pod_deleted = True
            except (KubeError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{resource}: {type(exc).__name__}: {exc}")
        if pod_deleted:
            try:
                kubectl(
                    "delete", "networkpolicy", self.name,
                    "--ignore-not-found", timeout=60,
                )
            except (KubeError, subprocess.TimeoutExpired) as exc:
                failures.append(f"networkpolicy: {type(exc).__name__}: {exc}")
        if failures:
            raise KubeError("egress proxy cleanup failed: " + "; ".join(failures))


def create_trial_secret(material) -> TrialSecret:
    """Create a unique Secret from in-memory ``CredentialMaterial``."""

    name = f"agent-credential-{uuid.uuid4().hex[:10]}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "labels": {"app": "agent-eval"}},
        "type": "Opaque",
        "stringData": material.values,
    }
    kubectl("apply", "-f", "-", input=json.dumps(manifest).encode(), timeout=30)
    return TrialSecret(name)


def egress_proxy_manifests(
    name: str, image: str, allowed_domains: list[str]
) -> list[dict]:
    """Build a Squid CONNECT proxy whose ACL is a DNS suffix allowlist."""

    if not allowed_domains:
        raise ValueError("proxy mode requires at least one allowed domain")
    acl = " ".join(sorted(set(allowed_domains)))
    config = (
        f"http_port {PROXY_PORT}\n"
        "acl SSL_ports port 443\n"
        "acl Safe_ports port 80 443\n"
        f"acl allowed_domains dstdomain {acl}\n"
        "http_access deny !Safe_ports\n"
        "http_access deny CONNECT !SSL_ports\n"
        "http_access allow allowed_domains\n"
        "http_access deny all\n"
        "access_log stdio:/dev/stdout\n"
        "cache deny all\n"
        "pid_filename /run/squid.pid\n"
        "coredump_dir /tmp\n"
    )
    labels = {"app": "agent-eval", "role": "egress-proxy", "proxy-id": name}
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "labels": labels},
            "data": {"squid.conf": config},
        },
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "automountServiceAccountToken": False,
                "enableServiceLinks": False,
                "activeDeadlineSeconds": PROXY_ACTIVE_DEADLINE_SECONDS,
                "securityContext": {
                    "runAsNonRoot": True,
                    "runAsUser": 13,
                    "runAsGroup": 13,
                    "fsGroup": 13,
                    "fsGroupChangePolicy": "OnRootMismatch",
                },
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": "proxy",
                        "image": image,
                        "imagePullPolicy": "IfNotPresent",
                        "ports": [{"name": "proxy", "containerPort": PROXY_PORT}],
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                            "seccompProfile": {"type": "RuntimeDefault"},
                            "runAsNonRoot": True,
                            "runAsUser": 13,
                            "runAsGroup": 13,
                            "readOnlyRootFilesystem": True,
                        },
                        "volumeMounts": [
                            {
                                "name": "config",
                                "mountPath": "/etc/squid/squid.conf",
                                "subPath": "squid.conf",
                                "readOnly": True,
                            },
                            {"name": "run", "mountPath": "/run"},
                            {"name": "logs", "mountPath": "/var/log/squid"},
                            {"name": "spool", "mountPath": "/var/spool/squid"},
                            {"name": "tmp", "mountPath": "/tmp"},
                        ],
                        "resources": {
                            "requests": {"cpu": "25m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "256Mi"},
                        },
                    }
                ],
                "volumes": [
                    {"name": "config", "configMap": {"name": name}},
                    {"name": "run", "emptyDir": {}},
                    {"name": "logs", "emptyDir": {}},
                    {"name": "spool", "emptyDir": {}},
                    {"name": "tmp", "emptyDir": {}},
                ],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "selector": {"proxy-id": name},
                "ports": [{"name": "proxy", "port": PROXY_PORT,
                           "targetPort": "proxy"}],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "podSelector": {"matchLabels": {"proxy-id": name}},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "podSelector": {
                                    "matchLabels": {"egress-proxy": name}
                                }
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": PROXY_PORT}],
                    }
                ],
            },
        },
    ]


def create_egress_proxy(image: str, allowed_domains: list[str]) -> EgressProxy:
    name = f"egress-{uuid.uuid4().hex[:8]}"
    try:
        for manifest in egress_proxy_manifests(name, image, allowed_domains):
            kubectl(
                "apply", "-f", "-", input=json.dumps(manifest).encode(), timeout=60
            )
        service = kubectl("get", "service", name, "-o", "json", timeout=30)
        cluster_ip = json.loads(service.stdout)["spec"]["clusterIP"]
        ipaddress.ip_address(cluster_ip)
        proxy = EgressProxy(name, cluster_ip)
        proxy.wait_ready()
    except Exception:
        EgressProxy(name, "127.0.0.1").delete()
        raise
    return proxy


def sandbox_egress_policy_manifest(
    name: str, mode: str, *, proxy_id: str | None = None
) -> dict:
    if mode not in ("deny", "proxy", "open"):
        raise ValueError("network policy mode must be deny, proxy, or open")
    egress = []
    if mode == "open":
        egress = [{}]
    elif mode == "proxy":
        if not proxy_id:
            raise ValueError("proxy egress requires a proxy id")
        egress = [
            {
                "to": [{"podSelector": {"matchLabels": {"proxy-id": proxy_id}}}],
                "ports": [{"protocol": "TCP", "port": PROXY_PORT}],
            },
        ]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": f"egress-{name}"},
        "spec": {
            "podSelector": {"matchLabels": {"sandbox-id": name}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": egress,
        },
    }


def sandbox_pod_manifest(name: str, prefix: str, image: str, *,
                         env_from_secret: str | None = None,
                         credential_env_keys: tuple[str, ...] = (),
                         credential_file_items: dict[str, str] | None = None,
                         extra_env: dict[str, str] | None = None,
                         active_deadline: int = 3600,
                         resources: dict[str, dict[str, str]] | None = None,
                         security: dict | None = None,
                         proxy_id: str | None = None) -> dict:
    """Return the auditable pod manifest used for agent and eval sandboxes.

    The service-account token and service discovery environment are disabled,
    while the container gets seccomp, no privilege escalation, no Linux
    capabilities, and bounded resources. The caller applies an open, denied,
    or proxy-only egress policy separately.
    """
    security = {**DEFAULT_SECURITY, **(security or {})}
    container_security = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
        "runAsNonRoot": security["run_as_non_root"],
        "runAsUser": security["run_as_user"],
        "runAsGroup": security["run_as_group"],
        "readOnlyRootFilesystem": security["read_only_root_filesystem"],
    }
    volume_mounts = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
        {"name": "home", "mountPath": "/home/agent"},
        {"name": "tests", "mountPath": "/tests"},
        {"name": "results", "mountPath": "/results"},
    ]
    volumes = [
        {"name": name, "emptyDir": {}}
        for name in ("workspace", "tmp", "home", "tests", "results")
    ]
    env = [
        {"name": "HOME", "value": "/home/agent"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    for key, value in sorted((extra_env or {}).items()):
        env.append({"name": key, "value": value})

    spec: dict = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "labels": {
            "app": "agent-eval", "role": "sandbox", "phase": prefix,
            "sandbox-id": name,
            **({"egress-proxy": proxy_id} if proxy_id else {}),
        }},
        "spec": {
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "securityContext": {
                "runAsNonRoot": security["run_as_non_root"],
                "runAsUser": security["run_as_user"],
                "runAsGroup": security["run_as_group"],
                "fsGroup": security["run_as_group"],
                "fsGroupChangePolicy": "OnRootMismatch",
            },
            "restartPolicy": "Never",
            "activeDeadlineSeconds": active_deadline,
            "terminationGracePeriodSeconds": 1,
            "containers": [{
                "name": "sandbox",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sh", "-c", "sleep infinity"],
                "workingDir": "/workspace",
                "env": env,
                "securityContext": container_security,
                "resources": deepcopy(
                    DEFAULT_SANDBOX_RESOURCES if resources is None else resources
                ),
                "volumeMounts": volume_mounts,
            }],
            "volumes": volumes,
        },
    }
    if env_from_secret:
        for key in credential_env_keys:
            env.append(
                {
                    "name": key,
                    "valueFrom": {
                        "secretKeyRef": {"name": env_from_secret, "key": key}
                    },
                }
            )
        file_items = credential_file_items or {}
        if file_items:
            volumes.append(
                {
                    "name": "credentials",
                    "secret": {
                        "secretName": env_from_secret,
                        "defaultMode": 288,
                        "items": [
                            {"key": key, "path": path}
                            for key, path in sorted(file_items.items())
                        ],
                    },
                }
            )
            volume_mounts.append(
                {
                    "name": "credentials",
                    "mountPath": CREDENTIAL_MOUNT,
                    "readOnly": True,
                }
            )
    return spec


def create_sandbox_pod(prefix: str, image: str, *, env_from_secret: str | None = None,
                       credential_env_keys: tuple[str, ...] = (),
                       credential_file_items: dict[str, str] | None = None,
                       extra_env: dict[str, str] | None = None,
                       active_deadline: int = 3600,
                       resources: dict[str, dict[str, str]] | None = None,
                       security: dict | None = None,
                       egress_mode: str = "open",
                       proxy_id: str | None = None) -> Pod:
    """Create a sleeping pod we can copy files into and exec commands in."""
    name = f"{prefix}-{uuid.uuid4().hex[:8]}"
    spec = sandbox_pod_manifest(
        name, prefix, image, env_from_secret=env_from_secret,
        credential_env_keys=credential_env_keys,
        credential_file_items=credential_file_items,
        extra_env=extra_env,
        active_deadline=active_deadline, resources=resources, security=security,
        proxy_id=proxy_id,
    )
    policy = sandbox_egress_policy_manifest(
        name, egress_mode, proxy_id=proxy_id
    )
    policy_name = policy["metadata"]["name"]
    kubectl("apply", "-f", "-", input=json.dumps(policy).encode(), timeout=60)
    try:
        kubectl("apply", "-f", "-", input=json.dumps(spec).encode(), timeout=60)
    except Exception:
        # An apply timeout is ambiguous: the API server may have created the
        # pod even though kubectl did not receive the response. Delete by the
        # deterministic name before dropping its policy.
        with suppress(Exception):
            kubectl(
                "delete", "pod", name, "--ignore-not-found", "--wait=true",
                check=False, timeout=60,
            )
        if policy_name:
            with suppress(Exception):
                kubectl(
                    "delete", "networkpolicy", policy_name,
                    "--ignore-not-found", check=False, timeout=30,
                )
        raise
    return Pod(name, network_policy_name=policy_name)
