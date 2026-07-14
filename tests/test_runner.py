import json
import subprocess

import pytest

from agent_eval import metrics, runner
from agent_eval.evaluators.tests import TestResults as EvalTestResults
from agent_eval.kube import KubeError, UnsafeArchiveError
from agent_eval.metrics import AgentMetrics
from agent_eval.credentials import CredentialMaterial
from agent_eval.task import SandboxResources, load_task

IMAGE_DIGEST = "sha256:" + "a" * 64


class _EvalOOMPod:
    def __init__(self):
        self.deleted = False

    def wait_ready(self):
        return None

    def copy_dir_to(self, local_dir, remote_dir):
        return None

    def copy_dir_from(self, remote_dir, local_dir):
        raise AssertionError("results must not be trusted after an OOM termination")

    def exec(self, command, timeout=None, env=None):
        if command in (
            "rm -rf /workspace && mkdir -p /workspace",
            "mkdir -p /results",
        ):
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(command, 137, stdout=b"", stderr=b"Killed")

    def infrastructure_failure(self, command_exit_code=None):
        if command_exit_code == 137:
            return "OOMKilled container exceeded its memory limit"
        return None

    def image_digest(self):
        return IMAGE_DIGEST

    def delete(self):
        self.deleted = True


class _AgentDeadlinePod:
    def __init__(self):
        self.deleted = False

    def wait_ready(self):
        raise KubeError("pod stopped before becoming ready")

    def infrastructure_failure(self, command_exit_code=None):
        return "DeadlineExceeded pod was active longer than its deadline"

    def delete(self):
        self.deleted = True


class _SuccessfulEvalPod:
    def __init__(self, test_exit_code=0):
        self.test_exit_code = test_exit_code
        self.deleted = False
        self.events = []

    def wait_ready(self):
        self.events.append(("ready",))

    def copy_dir_to(self, local_dir, remote_dir):
        self.events.append(("copy_to", remote_dir))

    def copy_dir_from(self, remote_dir, local_dir):
        self.events.append(("copy_from", remote_dir))
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "junit.xml").write_text(
            '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase classname="t" name="ok"/>'
            "</testsuite></testsuites>"
        )

    def exec(self, command, timeout=None, env=None):
        self.events.append(("exec", command))
        code = (
            self.test_exit_code
            if ".agent-eval-pytest.py" in command
            or command.startswith("cd /workspace")
            else 0
        )
        return subprocess.CompletedProcess(command, code, stdout=b"", stderr=b"")

    def infrastructure_failure(self, command_exit_code=None):
        return None

    def image_digest(self):
        return IMAGE_DIGEST

    def delete(self):
        self.deleted = True


class _AgentTimeoutPod:
    def __init__(self):
        self.deleted = False

    def wait_ready(self):
        return None

    def copy_dir_to(self, local_dir, remote_dir):
        return None

    def copy_dir_from(self, remote_dir, local_dir):
        local_dir.mkdir(parents=True, exist_ok=True)

    def exec(self, command, timeout=None, env=None):
        raise subprocess.TimeoutExpired(command, timeout, output=b"", stderr=b"")

    def infrastructure_failure(self, command_exit_code=None):
        return None

    def image_digest(self):
        return IMAGE_DIGEST

    def delete(self):
        self.deleted = True


def test_eval_resource_termination_is_infrastructure_evidence(monkeypatch, tmp_path):
    task = load_task("example-todo-api").model_copy(
        update={
            "resources": SandboxResources.model_validate(
                {"eval": {"limits": {"memory": "5Gi"}}}
            )
        }
    )
    pod = _EvalOOMPod()
    create_args = {}

    def fake_create(*args, **kwargs):
        create_args.update(kwargs)
        return pod

    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda tag: IMAGE_DIGEST)
    monkeypatch.setattr(runner, "create_sandbox_pod", fake_create)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "workspace").mkdir()

    result = runner.run_eval_phase(task, tmp_path / "workspace", run_dir)

    assert result.infra_error == (
        "eval sandbox infrastructure failure: "
        "OOMKilled container exceeded its memory limit"
    )
    assert create_args["resources"]["limits"]["memory"] == "5Gi"
    assert "Killed" in (run_dir / "eval-output.txt").read_text()
    assert pod.deleted is True


def test_cluster_image_check_requires_image_on_every_running_node(monkeypatch):
    nodes = {
        "name": "agent-eval",
        "nodes": [
            {"name": "server", "role": "server", "State": {"Running": True}},
            {"name": "agent", "role": "agent", "State": {"Running": True}},
            {"name": "lb", "role": "loadbalancer", "State": {"Running": True}},
        ],
    }
    missing = set()
    digests = {"server": IMAGE_DIGEST, "agent": IMAGE_DIGEST}

    def fake_run(command, **kwargs):
        if command[:4] == ["k3d", "cluster", "list", "-o"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps([nodes]), stderr=""
            )
        node = command[2]
        return subprocess.CompletedProcess(
            command,
            1 if node in missing else 0,
            stdout=json.dumps({"status": {"id": digests[node]}}),
            stderr="",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    assert runner._cluster_has_image("example:tag", IMAGE_DIGEST)
    digests["agent"] = "sha256:" + "b" * 64
    assert not runner._cluster_has_image("example:tag", IMAGE_DIGEST)
    digests["agent"] = IMAGE_DIGEST
    missing.add("agent")
    assert not runner._cluster_has_image("example:tag", IMAGE_DIGEST)


@pytest.mark.parametrize("test_exit_code, resolved", [(0, True), (1, False)])
def test_eval_replaces_workspace_and_requires_test_command_success(
    monkeypatch, tmp_path, test_exit_code, resolved
):
    task = load_task("example-todo-api")
    pod = _SuccessfulEvalPod(test_exit_code)
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda tag: IMAGE_DIGEST)
    monkeypatch.setattr(runner, "create_sandbox_pod", lambda *args, **kwargs: pod)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = runner.run_eval_phase(task, workspace, run_dir)

    assert pod.events[1] == (
        "exec",
        "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?*",
    )
    assert pod.events[2] == ("copy_to", "/workspace")
    assert result.command_exit_code == test_exit_code
    assert result.resolved is resolved
    assert pod.deleted is True


def test_eval_cleanup_failure_becomes_infrastructure_evidence(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")
    pod = _SuccessfulEvalPod()
    delete_attempts = 0

    def fail_delete():
        nonlocal delete_attempts
        delete_attempts += 1
        raise KubeError("API unavailable")

    pod.delete = fail_delete
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "create_sandbox_pod", lambda *args, **kwargs: pod)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = runner.run_eval_phase(task, workspace, run_dir)

    assert delete_attempts == 2
    assert not result.resolved
    assert "eval pod cleanup failed after 2 attempts" in result.infra_error
    assert "eval pod cleanup failed" in (run_dir / "eval-output.txt").read_text()


def test_eval_local_archive_failure_becomes_integrity_evidence(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")
    pod = _SuccessfulEvalPod()

    def refuse_copy(local_dir, remote_dir):
        del local_dir, remote_dir
        raise UnsafeArchiveError("local transfer tree expands beyond limit")

    pod.copy_dir_to = refuse_copy
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "create_sandbox_pod", lambda *args, **kwargs: pod)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = runner.run_eval_phase(task, workspace, run_dir)

    assert result.command_exit_code == 126
    assert "local transfer tree expands beyond limit" in result.integrity_error
    assert pod.deleted is True


def test_evaluate_workspace_promotes_eval_runtime_digest_to_provenance(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persisted = []

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_image", lambda task: None)
    monkeypatch.setattr(
        runner,
        "_capture_provenance",
        lambda task, record: setattr(
            record.provenance, "image_tag", task.image_tag
        ),
    )
    monkeypatch.setattr(
        runner,
        "run_eval_phase",
        lambda *args, **kwargs: EvalTestResults(
            total=1,
            passed=1,
            command_exit_code=0,
            runtime_image_digest=IMAGE_DIGEST,
        ),
    )
    monkeypatch.setattr(
        runner, "compute_diff", lambda *args: runner.DiffStats()
    )
    monkeypatch.setattr(
        runner, "_persist_run", lambda task, record: persisted.append(record)
    )

    record = runner.evaluate_workspace(
        task, workspace, run_scans=False, run_judge=False
    )

    assert record.provenance.eval_image_digest == IMAGE_DIGEST
    assert record.provenance.image_digest == IMAGE_DIGEST
    assert persisted == [record]


def test_agent_deadline_is_persisted_as_infrastructure_evidence(monkeypatch, tmp_path):
    task = load_task("example-todo-api").model_copy(
        update={
            "resources": SandboxResources.model_validate(
                {"agent": {"limits": {"cpu": "3"}}}
            )
        }
    )
    pod = _AgentDeadlinePod()
    create_args = {}
    saved = []

    def fake_create(*args, **kwargs):
        create_args.update(kwargs)
        return pod

    class Adapter:
        name = "test-agent"

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda tag: IMAGE_DIGEST)
    monkeypatch.setattr(runner, "create_sandbox_pod", fake_create)
    monkeypatch.setattr(runner, "save_run", saved.append)

    record = runner.run_agent_trial(task, Adapter())

    expected = (
        "agent sandbox infrastructure failure: "
        "DeadlineExceeded pod was active longer than its deadline"
    )
    assert record.efficiency.infra_error == expected
    assert record.correctness.infra_error == expected
    assert create_args["resources"]["limits"]["cpu"] == "3"
    assert saved == [record]
    assert pod.deleted is True


def test_agent_timeout_is_an_explicit_failed_outcome(monkeypatch, tmp_path):
    task = load_task("example-todo-api")
    pod = _AgentTimeoutPod()

    class Adapter:
        name = "test-agent"
        env = {}

        def build_command(self, model=None):
            return "run-agent"

        def parse_transcript(self, transcript):
            return AgentMetrics()

    def fake_evaluate(task, workspace, *, record, **kwargs):
        record.correctness = EvalTestResults(
            total=1,
            passed=1,
            command_exit_code=0,
            infra_error=record.efficiency.infra_error,
        )
        return record

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda tag: IMAGE_DIGEST)
    monkeypatch.setattr(runner, "create_sandbox_pod", lambda *args, **kwargs: pod)
    monkeypatch.setattr(runner, "evaluate_workspace", fake_evaluate)

    record = runner.run_agent_trial(task, Adapter())

    assert record.efficiency.timed_out is True
    assert record.efficiency.infra_error == "agent timed out after 900s"
    assert not record.correctness.resolved
    assert pod.deleted is True


def test_validate_task_rejects_a_starter_that_already_passes(monkeypatch):
    task = load_task("example-todo-api")
    monkeypatch.setattr(runner, "ensure_image", lambda task: None)
    monkeypatch.setattr(runner, "_image_digest", lambda tag: IMAGE_DIGEST)
    monkeypatch.setattr(
        runner,
        "run_eval_phase",
        lambda task, workspace, run_dir, **kwargs: EvalTestResults(
            total=1, passed=1, command_exit_code=0
        ),
    )

    with pytest.raises(ValueError, match="starter workspace must fail"):
        runner.validate_task(task)


def test_eval_rejects_submitted_pytest_shadow_before_starting_pod(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pytest.py").write_text(
        "open('/results/junit.xml', 'w').write('<testsuite tests=\"1\"/>')\n"
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        runner,
        "create_sandbox_pod",
        lambda *args, **kwargs: pytest.fail("unsafe workspace reached Kubernetes"),
    )

    result = runner.run_eval_phase(task, workspace, run_dir)

    assert not result.resolved
    assert result.command_exit_code == 126
    assert "evaluator-control path changed: pytest.py" == result.integrity_error


def test_workspace_symlinks_are_rejected_before_host_diffing(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe\n")
    (workspace / "alias.txt").symlink_to("safe.txt")

    error = runner._workspace_safety_error(workspace)

    assert error == "workspace symlink alias.txt is not allowed"


def test_judge_input_requires_successful_zero_secret_scan():
    from agent_eval.metrics import RunRecord, ScanResults

    record = RunRecord(run_id="r", task_id="t", agent="external")
    assert not runner._judge_input_is_safe(record)

    record.scans = ScanResults(
        secrets_found=1, scanner_status={"gitleaks": "ok"}
    )
    assert not runner._judge_input_is_safe(record)

    record.scans = ScanResults(
        secrets_found=0, scanner_status={"gitleaks": "ok"}
    )
    assert runner._judge_input_is_safe(record)


def test_deleted_starter_secret_blocks_coding_judge(
    monkeypatch, tmp_path
):
    from agent_eval.evaluators import judge, scanners
    from agent_eval.metrics import ScanResults

    task_dir = tmp_path / "task"
    starter = task_dir / "environment" / "workspace"
    tests_dir = task_dir / "tests"
    starter.mkdir(parents=True)
    tests_dir.mkdir()
    secret = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    (starter / "legacy.txt").write_text(f"token={secret}\n")
    (task_dir / "environment" / "Dockerfile").write_text("FROM scratch\n")
    task = load_task("example-todo-api").model_copy(update={"path": task_dir})
    produced = tmp_path / "produced"
    produced.mkdir()

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_image", lambda task: None)
    monkeypatch.setattr(runner, "_capture_provenance", lambda task, record: None)
    monkeypatch.setattr(
        runner,
        "run_eval_phase",
        lambda *args, **kwargs: EvalTestResults(
            total=1, passed=1, command_exit_code=0
        ),
    )

    def fake_scanners(scan_root, *args, **kwargs):
        screened_diff = (scan_root / ".agent-eval-workspace.diff").read_text()
        model_context = (
            scan_root / ".agent-eval-model-context.txt"
        ).read_text()
        assert secret in screened_diff
        assert "spec_adherence" in model_context
        return ScanResults(
            secrets_found=1,
            scanner_status={"gitleaks": "ok"},
        )

    monkeypatch.setattr(scanners, "run_scanners", fake_scanners)
    monkeypatch.setattr(
        judge,
        "run_judge",
        lambda *args, **kwargs: pytest.fail("deleted secret reached judge"),
    )

    record = runner.evaluate_workspace(task, produced)

    assert record.judge.weighted_score is None
    assert (record.run_dir / "judge-skipped.txt").is_file()


def test_credential_setup_failure_is_persisted_as_infrastructure_outcome(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")

    class Adapter:
        name = "codex"

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(
        runner,
        "load_trial_credentials",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("credential unavailable")
        ),
    )

    record = runner.run_agent_trial(task, Adapter())

    assert record.outcome.status == "infra_error"
    assert "credential unavailable" in record.efficiency.infra_error
    assert (record.run_dir / "results.json").is_file()
    assert (metrics.RUNS_ROOT / "metrics.db").is_file()


def test_cleanup_resources_are_independent_when_pod_delete_fails(
    monkeypatch, tmp_path
):
    task = load_task("example-todo-api")
    deleted = []

    class Adapter:
        name = "codex"

    class Secret:
        name = "secret"

        def delete(self):
            deleted.append("secret")

    class Proxy:
        name = "proxy"
        endpoint = "http://10.0.0.2:3128"

        def logs(self):
            return ""

        def delete(self):
            deleted.append("proxy")

    class FailingPod:
        def wait_ready(self):
            raise KubeError("not ready")

        def infrastructure_failure(self, command_exit_code=None):
            return "ImagePullBackOff"

        def delete(self):
            deleted.append("pod")
            raise subprocess.TimeoutExpired("delete", 60)

    monkeypatch.setattr(metrics, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runner, "ensure_namespace", lambda: None)
    monkeypatch.setattr(
        runner,
        "load_trial_credentials",
        lambda *args, **kwargs: CredentialMaterial(
            values={"codex-auth": "{}"},
            file_items={"codex-auth": "codex-auth.json"},
        ),
    )
    monkeypatch.setattr(runner, "create_trial_secret", lambda material: Secret())
    monkeypatch.setattr(
        runner, "create_egress_proxy", lambda *args, **kwargs: Proxy()
    )
    monkeypatch.setattr(
        runner, "create_sandbox_pod", lambda *args, **kwargs: FailingPod()
    )

    record = runner.run_agent_trial(task, Adapter())

    assert record.outcome.status == "infra_error"
    assert deleted == ["pod", "pod", "proxy", "secret"]
    assert "agent pod cleanup failed after 2 attempts" in record.efficiency.infra_error
