"""Run pipeline: (agent phase) -> snapshot/diff -> eval phase -> scans -> judge.

Eval starts in a fresh pod so state from the agent pod cannot persist into it.
Produced code still executes in the eval pod with access to its test inputs and
result paths."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path

from rich.console import Console

from .attestation import (
    capture_git_state,
    create_attestation,
    hash_tree,
    sha256_file,
)
from .cluster import build_and_import_image
from .credentials import load_trial_credentials
from .evaluators.tests import TestResults, parse_coverage, parse_junit
from .kube import (
    CommandOutputLimitError,
    KubeError,
    Pod,
    UnsafeArchiveError,
    create_egress_proxy,
    create_sandbox_pod,
    create_trial_secret,
    ensure_namespace,
)
from .metrics import DiffStats, RunRecord, now_iso, save_run
from .outcome import evaluate_outcome
from .task import Task

console = Console()
REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVIDER_DOMAINS = {
    "claude-code": [".anthropic.com", ".claude.ai"],
    "codex": [".openai.com", ".chatgpt.com"],
}
_TRUSTED_PYTEST_RUNNER = Path(__file__).parent / "evaluators" / "trusted_pytest.py"
_EVALUATOR_CONTROL_FILES = {
    "conftest.py",
    "pytest.py",
    "sitecustomize.py",
    "usercustomize.py",
}
_EVALUATOR_CONTROL_PACKAGES = {"_pytest", "coverage", "pluggy", "pytest_cov"}


def _sandbox_infra_error(phase: str, pod: Pod,
                         command_exit_code: int | None = None) -> str | None:
    evidence = pod.infrastructure_failure(command_exit_code)
    if evidence is None:
        return None
    return f"{phase} sandbox infrastructure failure: {evidence}"


def new_run_id(task: Task, agent: str) -> str:
    return f"{task.id}--{agent}--{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _cluster_has_image(tag: str, expected_digest: str) -> bool:
    """Return whether every running node resolves tag to the host digest."""

    listed = subprocess.run(
        ["k3d", "cluster", "list", "-o", "json"],
        capture_output=True,
        text=True,
    )
    if listed.returncode != 0:
        return False
    try:
        clusters = json.loads(listed.stdout)
        cluster = next(
            item
            for item in clusters
            if isinstance(item, dict) and item.get("name") == "agent-eval"
        )
        nodes = [
            node["name"]
            for node in cluster.get("nodes", [])
            if node.get("role") in {"server", "agent"}
            and node.get("State", {}).get("Running") is True
        ]
    except (json.JSONDecodeError, KeyError, StopIteration, TypeError):
        return False
    if not nodes:
        return False
    for node in nodes:
        inspected = subprocess.run(
            ["docker", "exec", node, "crictl", "inspecti", tag],
            capture_output=True,
            text=True,
        )
        if inspected.returncode != 0:
            return False
        try:
            actual = json.loads(inspected.stdout)["status"]["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return False
        if not isinstance(actual, str) or actual.lower() != expected_digest.lower():
            return False
    return True


def ensure_image(task: Task, rebuild: bool = False) -> None:
    if not rebuild:
        local_digest = _image_digest(task.image_tag)
        if local_digest is not None:
            if _cluster_has_image(task.image_tag, local_digest):
                return
            # The image exists only in the host daemon after a cluster recreate.
            imported = subprocess.run(
                ["k3d", "image", "import", task.image_tag, "-c", "agent-eval"],
                capture_output=True,
                text=True,
            )
            if imported.returncode != 0:
                raise KubeError(
                    "could not import the task image into k3d: "
                    f"{imported.stderr[-1000:]}"
                )
            if not _cluster_has_image(task.image_tag, local_digest):
                raise KubeError(
                    "imported task image digest does not match the host image "
                    "on every running k3d node"
                )
            return
    build_and_import_image(str(task.environment_dir), task.image_tag)
    local_digest = _image_digest(task.image_tag)
    if local_digest is None or not _cluster_has_image(
        task.image_tag, local_digest
    ):
        raise KubeError(
            "built task image digest does not match on every running k3d node"
        )


def _image_digest(tag: str) -> str | None:
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format={{.Id}}", tag],
        capture_output=True,
        text=True,
    )
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value.startswith("sha256:") else None


def _tool_version(command: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    output = (proc.stdout or proc.stderr).strip().splitlines()
    return output[0][:300] if output else None


def _capture_provenance(task: Task, record: RunRecord) -> None:
    with suppress(Exception):
        git = capture_git_state(REPO_ROOT)
        record.provenance.harness_commit = git.sha
        record.provenance.harness_dirty = git.dirty
        record.provenance.harness_worktree_sha256 = git.worktree_sha256
    with suppress(Exception):
        record.provenance.task_tree_sha256 = hash_tree(task.path)
    record.provenance.image_tag = task.image_tag
    record.provenance.local_image_digest = _image_digest(task.image_tag)
    record.provenance.agent_image_digest = record.efficiency.runtime_image_digest
    record.provenance.eval_image_digest = record.correctness.runtime_image_digest
    record.provenance.image_digest = (
        record.correctness.runtime_image_digest
        or record.efficiency.runtime_image_digest
    )
    record.provenance.tool_versions = {
        "python": sys.version.split()[0],
        "docker": _tool_version(["docker", "--version"]),
        "kubectl": _tool_version(["kubectl", "version", "--client"]),
        "k3d": _tool_version(["k3d", "version"]),
        "egress-proxy-image": (
            task.network.proxy_image
            if task.network.agent_mode == "proxy" else None
        ),
    }


def _attestable_artifacts(run_dir: Path) -> list[str]:
    artifacts = []
    for path in sorted(run_dir.rglob("*")):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.name not in ("attestation.json", "attestation.json.sha256")
        ):
            artifacts.append(path.relative_to(run_dir).as_posix())
    return artifacts


def _persist_run(task: Task, record: RunRecord) -> None:
    """Persist the record, then bind its artifacts into unsigned provenance."""

    save_run(record)
    provenance = record.provenance
    if not (
        provenance.image_tag
        and provenance.image_digest
        and provenance.harness_commit
        and provenance.harness_dirty is not None
        and provenance.harness_worktree_sha256
    ):
        return
    try:
        create_attestation(
            statement_path=record.run_dir / "attestation.json",
            artifact_root=record.run_dir,
            artifact_paths=_attestable_artifacts(record.run_dir),
            task_root=task.path,
            task_id=task.id,
            image_tag=provenance.image_tag,
            image_digest=provenance.image_digest,
            harness_git_sha=provenance.harness_commit,
            harness_git_dirty=provenance.harness_dirty,
            harness_git_worktree_sha256=provenance.harness_worktree_sha256,
            models={
                "agent": record.efficiency.model,
                "agent-requested": record.efficiency.requested_model,
                "judge": record.judge.model,
            },
            tool_versions=provenance.tool_versions,
            outcome=(record.outcome.model_dump(mode="json") if record.outcome else {}),
        )
    except Exception as exc:
        # The run remains usable, but absence of attestation is visible to a
        # verifier and can be made a CI gate by invoking verify-run.
        console.print(f"[yellow]could not create run attestation: {exc}[/yellow]")


def _delete_with_retries(resource: object, label: str, attempts: int = 2) -> str | None:
    """Delete one trial resource, retrying while preserving a final failure."""

    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            resource.delete()
            return None
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    return (
        f"{label} cleanup failed after {attempts} attempts: "
        f"{type(last_error).__name__}: {str(last_error)[:500]}"
    )


def _judge_input_is_safe(record: RunRecord) -> bool:
    """Require complete zero-secret evidence before sending a diff to a model."""

    return (
        record.scans.scanner_status.get("gitleaks") == "ok"
        and record.scans.secrets_found == 0
    )


def _workspace_safety_error(workspace: Path) -> str | None:
    """Reject host-dangerous trees before diffing or invoking scanners."""

    try:
        root = workspace.resolve(strict=True)
    except OSError as exc:
        return f"workspace is unavailable: {type(exc).__name__}"
    if not root.is_dir():
        return "workspace is not a directory"

    def visit(directory: Path) -> str | None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            return f"workspace directory is unreadable: {type(exc).__name__}"
        for entry in entries:
            relative = Path(entry.path).relative_to(root).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return f"workspace path {relative} is unreadable: {type(exc).__name__}"
            if stat.S_ISLNK(metadata.st_mode):
                return f"workspace symlink {relative} is not allowed"
            elif stat.S_ISDIR(metadata.st_mode):
                if error := visit(Path(entry.path)):
                    return error
            elif not stat.S_ISREG(metadata.st_mode):
                return f"workspace special file {relative} is not allowed"
        return None

    return visit(root)


def _control_paths(root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    if not root.is_dir():
        return paths
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if (
            candidate.name in _EVALUATOR_CONTROL_FILES
            or relative.parts[0] in _EVALUATOR_CONTROL_PACKAGES
        ):
            paths[relative.as_posix()] = candidate
    return paths


def _path_signature(path: Path | None) -> str:
    if path is None or not os.path.lexists(path):
        return "absent"
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if path.is_symlink():
        return f"link:{mode:o}:{path.readlink()}"
    if path.is_dir():
        return f"dir:{mode:o}:{hash_tree(path)}"
    return f"file:{mode:o}:{sha256_file(path)}"


def _evaluator_control_error(task: Task, workspace: Path) -> str | None:
    starter = _control_paths(task.workspace_dir)
    produced = _control_paths(workspace)
    for relative in sorted(set(starter) | set(produced)):
        if _path_signature(starter.get(relative)) != _path_signature(
            produced.get(relative)
        ):
            return f"evaluator-control path changed: {relative}"
    return None


def _trusted_test_command(command: str) -> tuple[str, bool]:
    """Replace ordinary pytest startup with an isolated trusted bootstrap."""

    try:
        argv = shlex.split(command)
    except ValueError:
        return f"cd /workspace && {command}", False
    arguments: list[str] | None = None
    if (
        len(argv) >= 3
        and Path(argv[0]).name.startswith("python")
        and argv[1:3] == ["-m", "pytest"]
    ):
        arguments = argv[3:]
    elif argv and Path(argv[0]).name in {"pytest", "py.test"}:
        arguments = argv[1:]
    if arguments is None:
        return f"cd /workspace && {command}", False
    trusted = [
        "python",
        "-I",
        "/tests/.agent-eval-pytest.py",
        *arguments,
        "-c",
        "/dev/null",
        "--rootdir=/tests",
    ]
    return shlex.join(trusted), True


def _runtime_image_evidence(pod: Pod, expected: str | None) -> tuple[str | None, str | None]:
    try:
        actual = pod.image_digest()
    except (KubeError, subprocess.TimeoutExpired):
        actual = None
    if actual is None:
        return None, "runtime task image digest is unavailable"
    if expected is not None and actual != expected:
        return actual, f"runtime image digest {actual} does not match expected {expected}"
    return actual, None


def run_eval_phase(
    task: Task,
    workspace: Path,
    run_dir: Path,
    *,
    expected_runtime_digest: str | None = None,
) -> TestResults:
    """Copy a produced workspace + hidden tests into a fresh pod, run the task's
    test command, and pull back /results for parsing."""
    integrity_error = _workspace_safety_error(workspace) or _evaluator_control_error(
        task, workspace
    )
    if integrity_error:
        (run_dir / "eval-output.txt").write_text(f"REJECTED: {integrity_error}\n")
        return TestResults(
            command_exit_code=126,
            integrity_error=integrity_error,
            failures=[integrity_error],
        )

    ensure_namespace()
    pod = create_sandbox_pod(
        "eval",
        task.image_tag,
        active_deadline=task.timeouts.eval_seconds + 900,
        resources=task.resources.eval.as_kubernetes(),
        security=task.security.model_dump(),
        egress_mode="deny",
    )
    result: TestResults | None = None

    def finish(**kwargs) -> TestResults:
        nonlocal result
        result = TestResults(**kwargs)
        return result

    try:
        try:
            pod.wait_ready()
            runtime_digest, image_error = _runtime_image_evidence(
                pod, expected_runtime_digest
            )
            if image_error:
                return finish(
                    infra_error=f"eval sandbox infrastructure failure: {image_error}",
                    runtime_image_digest=runtime_digest,
                )
            reset = pod.exec(
                "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?*",
                timeout=30,
            )
            if reset.returncode != 0:
                error = _sandbox_infra_error("eval", pod, reset.returncode)
                if error is None:
                    error = (
                        "could not reset eval workspace before copying the "
                        f"produced tree (exit {reset.returncode})"
                    )
                (run_dir / "eval-output.txt").write_text(f"{error}\n")
                return finish(
                    command_exit_code=reset.returncode,
                    infra_error=error,
                    runtime_image_digest=runtime_digest,
                )
            pod.copy_dir_to(workspace, "/workspace")
            pod.copy_dir_to(task.tests_dir, "/tests")
            test_command, trusted_pytest = _trusted_test_command(task.test_command)
            if trusted_pytest:
                with tempfile.TemporaryDirectory(
                    prefix="agent-eval-pytest-runner-"
                ) as tmp:
                    runner_dir = Path(tmp)
                    shutil.copy2(
                        _TRUSTED_PYTEST_RUNNER,
                        runner_dir / ".agent-eval-pytest.py",
                    )
                    pod.copy_dir_to(runner_dir, "/tests")
            results_ready = pod.exec("mkdir -p /results", timeout=30)
            if results_ready.returncode != 0:
                error = (
                    "eval sandbox infrastructure failure: could not prepare "
                    f"result directory (exit {results_ready.returncode})"
                )
                return finish(
                    command_exit_code=results_ready.returncode,
                    infra_error=error,
                    runtime_image_digest=runtime_digest,
                )

            console.print("running hidden tests in eval pod...")
            try:
                proc = pod.exec(
                    test_command,
                    timeout=task.timeouts.eval_seconds,
                )
                output = (
                    proc.stdout.decode(errors="replace")
                    + proc.stderr.decode(errors="replace")
                )
            except subprocess.TimeoutExpired:
                error = _sandbox_infra_error("eval", pod)
                if error is None:
                    error = (
                        f"test command timed out after {task.timeouts.eval_seconds}s"
                    )
                (run_dir / "eval-output.txt").write_text(f"TIMEOUT\n{error}\n")
                return finish(
                    infra_error=error, runtime_image_digest=runtime_digest
                )
            except CommandOutputLimitError as exc:
                output = (
                    exc.stdout.decode(errors="replace")
                    + exc.stderr.decode(errors="replace")
                )
                error = str(exc)
                (run_dir / "eval-output.txt").write_text(
                    output + f"\nOUTPUT CAP REACHED\n{error}\n"
                )
                return finish(
                    infra_error=error, runtime_image_digest=runtime_digest
                )
            (run_dir / "eval-output.txt").write_text(output)

            error = _sandbox_infra_error("eval", pod, proc.returncode)
            if error is not None:
                return finish(
                    command_exit_code=proc.returncode,
                    infra_error=error,
                    runtime_image_digest=runtime_digest,
                )

            results_dir = run_dir / "results"
            try:
                pod.copy_dir_from("/results", results_dir)
            except UnsafeArchiveError as exc:
                error = f"unsafe evaluator result archive: {exc}"
                return finish(
                    command_exit_code=126,
                    integrity_error=error,
                    failures=[error],
                    runtime_image_digest=runtime_digest,
                )
            test_results = parse_junit(
                results_dir / "junit.xml", command_exit_code=proc.returncode
            )
            test_results.runtime_image_digest = runtime_digest
            test_results.coverage_percent = parse_coverage(
                results_dir / "coverage.json"
            )
            result = test_results
            return result
        except UnsafeArchiveError as exc:
            error = f"unsafe evaluator archive: {exc}"
            return finish(
                command_exit_code=126,
                integrity_error=error,
                failures=[error],
            )
        except (KubeError, subprocess.TimeoutExpired) as exc:
            error = _sandbox_infra_error("eval", pod)
            if error is None:
                error = (
                    f"eval sandbox setup failed: {type(exc).__name__}: "
                    f"{str(exc)[:500]}"
                )
            (run_dir / "eval-output.txt").write_text(f"{error}\n")
            return finish(infra_error=error)
    finally:
        cleanup_error = _delete_with_retries(pod, "eval pod")
        if cleanup_error:
            with (run_dir / "eval-output.txt").open("a") as output_file:
                output_file.write(f"\n{cleanup_error}\n")
            if result is not None:
                if result.infra_error:
                    result.infra_error += f"; {cleanup_error}"
                else:
                    result.infra_error = cleanup_error


# derived artifacts the agent's tooling generates; excluded from diffing
JUNK_DIR_PATTERNS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".git",
                     "node_modules", ".venv", ".codex", ".claude", "*.pyc")


def compute_diff(starter: Path, produced: Path, run_dir: Path) -> DiffStats:
    """Diff the starter workspace against what the agent produced, ignoring
    derived artifacts (bytecode caches, agent config dirs, etc.)."""
    with tempfile.TemporaryDirectory(prefix="agent-eval-diff-") as tmp:
        ignore = shutil.ignore_patterns(*JUNK_DIR_PATTERNS)
        shutil.copytree(starter, Path(tmp) / "a", ignore=ignore, symlinks=True)
        shutil.copytree(produced, Path(tmp) / "b", ignore=ignore, symlinks=True)

        def git_diff(*flags: str) -> str:
            proc = subprocess.run(
                ["git", "-c", "core.quotePath=false", "diff", "--no-index",
                 *flags, "a", "b"],
                capture_output=True, text=True, cwd=tmp,
            )
            return proc.stdout

        (run_dir / "workspace.diff").write_text(git_diff())
        stats = DiffStats()
        for line in git_diff("--numstat").splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                stats.files_changed += 1
                if parts[0] != "-":  # binary files report "-"
                    stats.lines_added += int(parts[0])
                    stats.lines_removed += int(parts[1])
        return stats


def evaluate_workspace(task: Task, workspace: Path, *, agent: str = "external",
                       trial: int = 1, run_id: str | None = None,
                       record: RunRecord | None = None,
                       run_scans: bool = True, run_judge: bool = True) -> RunRecord:
    """Eval pipeline on an already-produced workspace: tests, diff, scans, judge."""
    if record is None:
        record = RunRecord(run_id=run_id or new_run_id(task, agent),
                           task_id=task.id, agent=agent, trial=trial,
                           started_at=now_iso())
    run_dir = record.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    integrity_error = _workspace_safety_error(workspace) or _evaluator_control_error(
        task, workspace
    )
    if integrity_error:
        (run_dir / "eval-output.txt").write_text(f"REJECTED: {integrity_error}\n")
        record.correctness = TestResults(
            command_exit_code=126,
            integrity_error=integrity_error,
            failures=[integrity_error],
        )
        record.finished_at = now_iso()
        _capture_provenance(task, record)
        record.outcome = evaluate_outcome(record, task.acceptance)
        _persist_run(task, record)
        return record

    ensure_image(task)
    _capture_provenance(task, record)
    record.correctness = run_eval_phase(
        task,
        workspace,
        run_dir,
        expected_runtime_digest=(
            record.efficiency.runtime_image_digest
            or record.provenance.local_image_digest
        ),
    )
    record.provenance.agent_image_digest = record.efficiency.runtime_image_digest
    record.provenance.eval_image_digest = record.correctness.runtime_image_digest
    record.provenance.image_digest = (
        record.correctness.runtime_image_digest
        or record.efficiency.runtime_image_digest
    )
    if record.efficiency.infra_error and record.correctness.infra_error is None:
        record.correctness.infra_error = record.efficiency.infra_error
    record.diff = compute_diff(task.workspace_dir, workspace, run_dir)

    if run_scans:
        from .evaluators.scanners import run_scanners

        # The judge receives the exact diff and task context, not only the
        # final workspace. Screen every dynamic prompt field so removed
        # credentials and secret-bearing diff metadata cannot reach it.
        with tempfile.TemporaryDirectory(
            prefix="agent-eval-judge-screen-"
        ) as tmp:
            scan_root = Path(tmp) / "workspace"
            shutil.copytree(workspace, scan_root)
            diff_text = (run_dir / "workspace.diff").read_text()
            (scan_root / ".agent-eval-workspace.diff").write_text(diff_text)
            (scan_root / ".agent-eval-model-context.txt").write_text(
                f"{task.prompt}\n{json.dumps(task.judge.weights, sort_keys=True)}\n"
            )
            record.scans = run_scanners(scan_root, run_dir, task.language)
            prefix = str(scan_root.resolve()) + "/"
            for finding in record.scans.findings:
                if isinstance(finding.get("path"), str):
                    finding["path"] = finding["path"].removeprefix(prefix)
        record.provenance.tool_versions.update(
            {
                f"scanner:{name}": version
                for name, version in record.scans.scanner_versions.items()
            }
        )
        record.provenance.tool_versions.update(
            {
                f"scanner-config:{name}": identity
                for name, identity in record.scans.scanner_configs.items()
            }
        )
    if run_judge and task.judge.enabled:
        if _judge_input_is_safe(record):
            from .evaluators.judge import run_judge as judge_workspace

            record.judge = judge_workspace(task, run_dir)
        else:
            reason = (
                "judge skipped: gitleaks must complete successfully with zero "
                "detected secrets before workspace.diff can be sent to a model"
            )
            (run_dir / "judge-skipped.txt").write_text(reason + "\n")
            console.print(f"[yellow]{reason}[/yellow]")

    if task.challenges:
        from .assurance import evaluate_challenges

        record.assurance = evaluate_challenges(
            task.challenges, workspace, run_dir, record
        )

    record.finished_at = now_iso()
    record.outcome = evaluate_outcome(record, task.acceptance)
    _persist_run(task, record)
    return record


def run_agent_trial(task: Task, adapter, *, trial: int = 1, model: str | None = None,
                    run_scans: bool = True, run_judge: bool = True,
                    experiment_id: str | None = None) -> RunRecord:
    """Full-harness trial: launch the coding agent in a sandbox pod, snapshot
    its workspace, then evaluate that workspace."""
    record = RunRecord(run_id=new_run_id(task, adapter.name), task_id=task.id,
                       agent=adapter.name, trial=trial,
                       experiment_id=experiment_id, started_at=now_iso())
    run_dir = record.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    material = None
    secret = None
    proxy = None
    pod = None
    produced = run_dir / "workspace"
    snapshot_available = False
    snapshot_integrity_error = None
    try:
        ensure_namespace()
        if adapter.name in _PROVIDER_DOMAINS or os.environ.get(
            "AGENT_EVAL_CREDENTIAL_COMMAND"
        ):
            material = load_trial_credentials(
                adapter.name,
                minimum_ttl_seconds=task.timeouts.agent_seconds + 300,
            )
        secret = create_trial_secret(material) if material else None
        if material:
            record.provenance.credential_source = material.source
            record.provenance.credential_mode = material.mode
            record.provenance.credential_expires_at = material.expires_at
        domains = sorted(
            set(_PROVIDER_DOMAINS.get(adapter.name, []))
            | set(task.network.allowed_domains)
        )
        if task.network.agent_mode == "proxy" and domains:
            proxy = create_egress_proxy(task.network.proxy_image, domains)
        proxy_env = {}
        if proxy:
            proxy_env = {
                "HTTP_PROXY": proxy.endpoint,
                "HTTPS_PROXY": proxy.endpoint,
                "http_proxy": proxy.endpoint,
                "https_proxy": proxy.endpoint,
            }
        pod = create_sandbox_pod(
            "agent",
            task.image_tag,
            env_from_secret=secret.name if secret else None,
            credential_env_keys=material.env_keys if material else (),
            credential_file_items=material.file_items if material else {},
            extra_env=proxy_env,
            active_deadline=task.timeouts.agent_seconds + 900,
            resources=task.resources.agent.as_kubernetes(),
            security=task.security.model_dump(),
            egress_mode=(
                "proxy" if proxy
                else "deny" if task.network.agent_mode == "proxy"
                else "open"
            ),
            proxy_id=proxy.name if proxy else None,
        )
        try:
            pod.wait_ready()
            expected_runtime_digest = _image_digest(task.image_tag)
            if expected_runtime_digest is None:
                raise KubeError("local task image digest is unavailable")
            runtime_digest, image_error = _runtime_image_evidence(
                pod, expected_runtime_digest
            )
            if image_error:
                raise KubeError(image_error)
            pod.copy_dir_to(task.workspace_dir, "/workspace")
            with tempfile.TemporaryDirectory() as tmp:
                from .agents import PROMPT_PATH
                prompt_dir = Path(tmp)
                (prompt_dir / Path(PROMPT_PATH).name).write_text(task.prompt)
                pod.copy_dir_to(prompt_dir, str(Path(PROMPT_PATH).parent))
            if hasattr(adapter, "prepare"):
                adapter.prepare(pod)

            console.print(f"running [bold]{adapter.name}[/bold] in sandbox pod "
                          f"(timeout {task.timeouts.agent_seconds}s)...")
            start = time.monotonic()
            timed_out = False
            try:
                proc = pod.exec(adapter.build_command(model),
                                timeout=task.timeouts.agent_seconds, env=adapter.env)
                (run_dir / "transcript.jsonl").write_bytes(proc.stdout)
                (run_dir / "agent-stderr.log").write_bytes(proc.stderr)
                record.efficiency = adapter.parse_transcript(
                    run_dir / "transcript.jsonl"
                )
                record.efficiency.requested_model = model
                record.efficiency.runtime_image_digest = runtime_digest
                record.efficiency.agent_exit_code = proc.returncode
                record.efficiency.infra_error = _sandbox_infra_error(
                    "agent", pod, proc.returncode
                )
            except subprocess.TimeoutExpired as e:
                timed_out = True
                (run_dir / "transcript.jsonl").write_bytes(e.stdout or b"")
                (run_dir / "agent-stderr.log").write_bytes(
                    (e.stderr or b"") + b"\nAGENT TIMED OUT")
                record.efficiency = adapter.parse_transcript(
                    run_dir / "transcript.jsonl"
                )
                record.efficiency.requested_model = model
                record.efficiency.runtime_image_digest = runtime_digest
                record.efficiency.timed_out = True
                record.efficiency.infra_error = _sandbox_infra_error("agent", pod)
                if record.efficiency.infra_error is None:
                    record.efficiency.infra_error = (
                        f"agent timed out after {task.timeouts.agent_seconds}s"
                    )
            except CommandOutputLimitError as exc:
                (run_dir / "transcript.jsonl").write_bytes(exc.stdout)
                (run_dir / "agent-stderr.log").write_bytes(
                    exc.stderr + b"\nAGENT OUTPUT CAP REACHED\n"
                )
                record.efficiency = adapter.parse_transcript(
                    run_dir / "transcript.jsonl"
                )
                record.efficiency.requested_model = model
                record.efficiency.runtime_image_digest = runtime_digest
                record.efficiency.infra_error = str(exc)
            record.efficiency.wall_time_s = round(time.monotonic() - start, 1)
            if timed_out:
                console.print("[yellow]agent timed out; evaluating partial work[/yellow]")

            try:
                pod.copy_dir_from("/workspace", produced)
            except UnsafeArchiveError as exc:
                snapshot_integrity_error = f"unsafe agent workspace archive: {exc}"
            else:
                snapshot_available = True
        except UnsafeArchiveError as exc:
            snapshot_integrity_error = f"unsafe agent workspace archive: {exc}"
        except (KubeError, subprocess.TimeoutExpired, RuntimeError, OSError, ValueError) as exc:
            error = _sandbox_infra_error("agent", pod)
            if error is None:
                error = f"agent trial setup failed: {type(exc).__name__}: {str(exc)[:500]}"
            record.efficiency.infra_error = error
    except UnsafeArchiveError as exc:
        snapshot_integrity_error = f"unsafe agent workspace archive: {exc}"
    except (KubeError, subprocess.TimeoutExpired, RuntimeError, OSError, ValueError) as exc:
        error = _sandbox_infra_error("agent", pod) if pod else None
        if error is None:
            error = f"agent trial setup failed: {type(exc).__name__}: {str(exc)[:500]}"
        record.efficiency.infra_error = error
    finally:
        if proxy:
            with suppress(Exception):
                (run_dir / "egress-proxy.log").write_text(proxy.logs())
        cleanup_errors = []
        if pod:
            if error := _delete_with_retries(pod, "agent pod"):
                cleanup_errors.append(error)
        if proxy:
            if error := _delete_with_retries(proxy, "egress proxy"):
                cleanup_errors.append(error)
        if secret:
            if error := _delete_with_retries(secret, "credential Secret"):
                cleanup_errors.append(error)
        if cleanup_errors:
            cleanup_error = "; ".join(cleanup_errors)
            if record.efficiency.infra_error:
                record.efficiency.infra_error += f"; {cleanup_error}"
            else:
                record.efficiency.infra_error = cleanup_error

    if not snapshot_available:
        if snapshot_integrity_error:
            record.correctness = TestResults(
                command_exit_code=126,
                integrity_error=snapshot_integrity_error,
                failures=[snapshot_integrity_error],
            )
        else:
            error = (
                record.efficiency.infra_error
                or "agent workspace snapshot unavailable"
            )
            record.correctness = TestResults(infra_error=error)
        record.finished_at = now_iso()
        _capture_provenance(task, record)
        record.outcome = evaluate_outcome(record, task.acceptance)
        _persist_run(task, record)
        return record

    return evaluate_workspace(task, produced, record=record,
                              run_scans=run_scans, run_judge=run_judge)


def validate_task(task: Task) -> RunRecord:
    """Overlay the oracle solution onto the starter workspace and require the
    hidden tests to pass. Proves the task + eval path work without any LLM."""
    problems = task.validate_layout()
    if problems:
        raise ValueError(f"task {task.id} layout invalid: {', '.join(problems)}")
    if not task.solution_dir.is_dir():
        raise ValueError(f"task {task.id} has no solution/ directory to validate with")

    ensure_image(task)
    expected_runtime_digest = _image_digest(task.image_tag)
    if expected_runtime_digest is None:
        raise ValueError(f"task {task.id} local image digest is unavailable")
    with tempfile.TemporaryDirectory(prefix="agent-eval-negative-control-") as tmp:
        baseline_dir = Path(tmp) / "baseline"
        baseline_dir.mkdir()
        baseline = run_eval_phase(
            task,
            task.workspace_dir,
            baseline_dir,
            expected_runtime_digest=expected_runtime_digest,
        )
    if baseline.infra_error:
        raise ValueError(
            f"task {task.id} starter negative control could not be evaluated: "
            f"{baseline.infra_error}"
        )
    if baseline.failed + baseline.errors == 0:
        raise ValueError(
            f"task {task.id} starter workspace must fail at least one hidden test"
        )

    with tempfile.TemporaryDirectory(prefix="agent-eval-oracle-") as tmp:
        oracle_ws = Path(tmp) / "workspace"
        shutil.copytree(task.workspace_dir, oracle_ws)
        shutil.copytree(task.solution_dir, oracle_ws, dirs_exist_ok=True)
        return evaluate_workspace(task, oracle_ws, agent="oracle",
                                  run_scans=False, run_judge=False)
