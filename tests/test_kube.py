import json
import subprocess

import pytest
from pydantic import ValidationError

from agent_eval.kube import DEFAULT_SANDBOX_RESOURCES, Pod, sandbox_pod_manifest
from agent_eval.task import PodResources, SandboxResources, load_task


def test_sandbox_manifest_removes_ambient_cluster_privilege():
    manifest = sandbox_pod_manifest(
        "agent-deadbeef",
        "agent",
        "agent-eval/example:latest",
        env_from_secret="agent-api-keys",
        active_deadline=123,
    )

    pod = manifest["spec"]
    container = pod["containers"][0]
    security = container["securityContext"]

    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["activeDeadlineSeconds"] == 123
    assert security == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["resources"] == DEFAULT_SANDBOX_RESOURCES
    assert container["envFrom"] == [
        {"secretRef": {"name": "agent-api-keys", "optional": True}}
    ]


def test_eval_manifest_does_not_receive_agent_secret():
    manifest = sandbox_pod_manifest(
        "eval-deadbeef", "eval", "agent-eval/example:latest"
    )

    container = manifest["spec"]["containers"][0]
    assert "envFrom" not in container
    assert manifest["metadata"]["labels"]["phase"] == "eval"


def test_task_resources_default_and_override_each_phase():
    task = load_task("example-todo-api")
    assert task.resources.agent.as_kubernetes() == DEFAULT_SANDBOX_RESOURCES
    assert task.resources.eval.as_kubernetes() == DEFAULT_SANDBOX_RESOURCES

    resources = SandboxResources.model_validate(
        {
            "agent": {"limits": {"memory": "6Gi"}},
            "eval": {
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"cpu": "4", "memory": "8Gi"},
            },
        }
    )

    assert resources.agent.limits.memory == "6Gi"
    assert resources.agent.requests.memory == "128Mi"
    assert resources.eval.as_kubernetes() == {
        "requests": {
            "cpu": "250m",
            "memory": "512Mi",
            "ephemeral-storage": "256Mi",
        },
        "limits": {
            "cpu": "4",
            "memory": "8Gi",
            "ephemeral-storage": "4Gi",
        },
    }


@pytest.mark.parametrize(
    "resources",
    [
        {"limits": {"memory": "not-a-quantity"}},
        {"limits": {"memory": "1K"}},
        {"requests": {"cpu": "3"}, "limits": {"cpu": "2"}},
        {"limits": {"gpu": "1"}},
    ],
)
def test_task_resources_reject_invalid_configuration(resources):
    with pytest.raises(ValidationError):
        PodResources.model_validate(resources)


def test_manifest_uses_custom_resources_without_mutating_them():
    resources = SandboxResources.model_validate(
        {"eval": {"limits": {"cpu": "3", "memory": "5Gi"}}}
    ).eval.as_kubernetes()

    manifest = sandbox_pod_manifest(
        "eval-deadbeef",
        "eval",
        "agent-eval/example:latest",
        resources=resources,
    )
    manifest["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "1"

    assert resources["limits"]["cpu"] == "3"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            {
                "containerStatuses": [
                    {"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}
                ]
            },
            "OOMKilled",
        ),
        (
            {
                "reason": "Evicted",
                "message": "The node was low on resource: ephemeral-storage.",
            },
            "Evicted",
        ),
        ({"reason": "DeadlineExceeded"}, "DeadlineExceeded"),
        (
            {
                "conditions": [
                    {
                        "reason": "Unschedulable",
                        "message": "0/1 nodes are available: Insufficient memory.",
                    }
                ]
            },
            "Insufficient memory",
        ),
        (
            {
                "conditions": [
                    {
                        "reason": "Unschedulable",
                        "message": (
                            "0/1 nodes are available: 1 node(s) had "
                            "untolerated taint"
                        ),
                    }
                ]
            },
            "untolerated taint",
        ),
    ],
)
def test_pod_preserves_infrastructure_failure_evidence(monkeypatch, status, expected):
    def fake_kubectl(*args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"status": status}).encode(), stderr=b""
        )

    monkeypatch.setattr("agent_eval.kube.kubectl", fake_kubectl)

    assert expected in Pod("eval-deadbeef").infrastructure_failure()


def test_pod_marks_sigkill_as_possible_resource_failure(monkeypatch):
    def fake_kubectl(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"gone")

    monkeypatch.setattr("agent_eval.kube.kubectl", fake_kubectl)

    evidence = Pod("agent-deadbeef").infrastructure_failure(command_exit_code=137)
    assert evidence == "command exited 137 (SIGKILL; resource-limit termination possible)"
