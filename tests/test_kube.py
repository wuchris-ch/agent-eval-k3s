from agent_eval.kube import DEFAULT_SANDBOX_RESOURCES, sandbox_pod_manifest


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
