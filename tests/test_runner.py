import subprocess

from agent_eval import metrics, runner
from agent_eval.kube import KubeError
from agent_eval.task import SandboxResources, load_task


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
        if command == "mkdir -p /results":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(command, 137, stdout=b"", stderr=b"Killed")

    def infrastructure_failure(self, command_exit_code=None):
        if command_exit_code == 137:
            return "OOMKilled container exceeded its memory limit"
        return None

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
    monkeypatch.setattr(runner, "create_sandbox_pod", fake_create)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    result = runner.run_eval_phase(task, tmp_path / "workspace", run_dir)

    assert result.infra_error == (
        "eval sandbox infrastructure failure: "
        "OOMKilled container exceeded its memory limit"
    )
    assert create_args["resources"]["limits"]["memory"] == "5Gi"
    assert "Killed" in (run_dir / "eval-output.txt").read_text()
    assert pod.deleted is True


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
