import io
import json
import subprocess
import tarfile

import pytest
from pydantic import ValidationError

from agent_eval.kube import (
    CommandOutputLimitError,
    DEFAULT_SANDBOX_RESOURCES,
    KubeError,
    Pod,
    TrialSecret,
    UnsafeArchiveError,
    create_sandbox_pod,
    egress_proxy_manifests,
    sandbox_egress_policy_manifest,
    sandbox_pod_manifest,
)
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
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "readOnlyRootFilesystem": True,
    }
    assert container["resources"] == DEFAULT_SANDBOX_RESOURCES
    assert container["env"][:2] == [
        {"name": "HOME", "value": "/home/agent"},
        {"name": "TMPDIR", "value": "/tmp"},
    ]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert {mount["mountPath"] for mount in container["volumeMounts"]} >= {
        "/workspace", "/tmp", "/home/agent", "/tests", "/results"
    }


def test_eval_manifest_does_not_receive_agent_secret():
    manifest = sandbox_pod_manifest(
        "eval-deadbeef", "eval", "agent-eval/example:latest"
    )

    container = manifest["spec"]["containers"][0]
    assert all("valueFrom" not in item for item in container["env"])
    assert manifest["metadata"]["labels"]["phase"] == "eval"


def test_governed_manifest_never_pulls_and_rejects_unknown_policy():
    image = "agent-eval/example:governed-" + "a" * 64
    manifest = sandbox_pod_manifest(
        "agent-deadbeef",
        "agent",
        image,
        image_pull_policy="Never",
    )

    assert manifest["spec"]["containers"][0]["image"] == image
    assert manifest["spec"]["containers"][0]["imagePullPolicy"] == "Never"
    with pytest.raises(ValueError, match="image pull policy"):
        sandbox_pod_manifest(
            "agent-deadbeef",
            "agent",
            image,
            image_pull_policy="Always",
        )


def test_running_image_manifest_resolves_cri_repo_digest_not_config_id(monkeypatch):
    from agent_eval import kube

    image_ref = "agent-eval/example:governed-" + "a" * 64
    manifest_digest = "sha256:" + "a" * 64
    config_digest = "sha256:" + "c" * 64
    pod_value = {
        "spec": {
            "nodeName": "k3d-agent-eval-server-0",
            "containers": [{"image": image_ref}],
        },
        "status": {"containerStatuses": [{"imageID": config_digest}]},
    }

    monkeypatch.setattr(
        kube,
        "kubectl",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(pod_value), stderr=""
        ),
    )

    def fake_run(command, **kwargs):
        assert command == [
            "docker",
            "exec",
            "k3d-agent-eval-server-0",
            "crictl",
            "inspecti",
            config_digest,
        ]
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "status": {
                        "id": config_digest,
                        "repoDigests": [
                            f"docker.io/agent-eval/example@{manifest_digest}"
                        ],
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(kube.subprocess, "run", fake_run)

    assert Pod("agent-one").image_manifest_digest(image_ref) == manifest_digest


def test_credential_secret_projects_only_declared_env_and_files():
    manifest = sandbox_pod_manifest(
        "agent-deadbeef",
        "agent",
        "agent-eval/example:contenthash",
        env_from_secret="trial-secret",
        credential_env_keys=("ANTHROPIC_API_KEY",),
        credential_file_items={"codex-auth": "codex-auth.json"},
    )

    container = manifest["spec"]["containers"][0]
    projected = [item for item in container["env"] if "valueFrom" in item]
    assert projected == [
        {
            "name": "ANTHROPIC_API_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "trial-secret",
                    "key": "ANTHROPIC_API_KEY",
                }
            },
        }
    ]
    credential_volume = next(
        volume for volume in manifest["spec"]["volumes"]
        if volume["name"] == "credentials"
    )
    assert credential_volume["secret"]["items"] == [
        {"key": "codex-auth", "path": "codex-auth.json"}
    ]


def test_eval_egress_is_empty_and_proxy_egress_is_narrow():
    denied = sandbox_egress_policy_manifest("eval-one", "deny")
    proxied = sandbox_egress_policy_manifest(
        "agent-one", "proxy", proxy_id="egress-one"
    )

    assert denied["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert denied["spec"]["ingress"] == []
    assert denied["spec"]["egress"] == []
    assert denied["spec"]["podSelector"]["matchLabels"] == {
        "sandbox-id": "eval-one"
    }
    proxy_rules = proxied["spec"]["egress"]
    assert len(proxy_rules) == 1
    assert proxy_rules[0]["to"] == [
        {"podSelector": {"matchLabels": {"proxy-id": "egress-one"}}}
    ]
    assert proxy_rules[0]["ports"] == [{"protocol": "TCP", "port": 3128}]


def test_domain_proxy_config_is_default_deny_with_explicit_suffixes():
    manifests = egress_proxy_manifests(
        "egress-one", "ubuntu/squid:example", [".openai.com", ".chatgpt.com"]
    )
    config = manifests[0]["data"]["squid.conf"]

    assert "acl allowed_domains dstdomain .chatgpt.com .openai.com" in config
    assert "http_access allow allowed_domains" in config
    assert "access_log stdio:/dev/stdout" in config
    assert "http_access deny all" in config
    assert "0.0.0.0/0" not in config
    proxy_pod = manifests[1]
    assert proxy_pod["spec"]["activeDeadlineSeconds"] == 3600
    ingress_policy = manifests[3]
    assert ingress_policy["spec"]["podSelector"]["matchLabels"] == {
        "proxy-id": "egress-one"
    }
    assert ingress_policy["spec"]["ingress"][0]["from"] == [
        {"podSelector": {"matchLabels": {"egress-proxy": "egress-one"}}}
    ]


def test_proxy_client_label_is_bound_to_its_trial_proxy():
    manifest = sandbox_pod_manifest(
        "agent-one", "agent", "image", proxy_id="egress-one"
    )

    assert manifest["metadata"]["labels"]["role"] == "sandbox"
    assert manifest["metadata"]["labels"]["egress-proxy"] == "egress-one"


def test_ambiguous_pod_apply_failure_cleans_pod_before_policy(monkeypatch):
    calls = []
    apply_count = 0

    def fake_kubectl(*args, **kwargs):
        nonlocal apply_count
        calls.append(args)
        if args[:3] == ("apply", "-f", "-"):
            apply_count += 1
            if apply_count == 2:
                raise subprocess.TimeoutExpired(args, 60)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("agent_eval.kube.kubectl", fake_kubectl)

    with pytest.raises(subprocess.TimeoutExpired):
        create_sandbox_pod("eval", "example:image", egress_mode="deny")

    cleanup = [call for call in calls if call and call[0] == "delete"]
    assert cleanup[0][1] == "pod"
    assert cleanup[0][2].startswith("eval-")
    assert cleanup[1][1:3] == (
        "networkpolicy",
        f"egress-{cleanup[0][2]}",
    )


def test_pod_snapshot_rejects_link_escape_without_extracting(monkeypatch, tmp_path):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo("./escape")
        member.type = tarfile.SYMTYPE
        member.linkname = "/etc/passwd"
        archive.addfile(member)

    monkeypatch.setattr(
        "agent_eval.kube._pod_archive_stream",
        lambda *args: io.BytesIO(payload.getvalue()),
    )

    with pytest.raises(UnsafeArchiveError, match="unsafe archive member"):
        Pod("agent-one").copy_dir_from("/workspace", tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()


def test_pod_snapshot_extracts_regular_files(monkeypatch, tmp_path):
    payload = io.BytesIO()
    content = b"safe\n"
    with tarfile.open(fileobj=payload, mode="w") as archive:
        member = tarfile.TarInfo("./safe.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))

    monkeypatch.setattr(
        "agent_eval.kube._pod_archive_stream",
        lambda *args: io.BytesIO(payload.getvalue()),
    )

    target = tmp_path / "snapshot"
    Pod("agent-one").copy_dir_from("/workspace", target)

    assert (target / "safe.txt").read_bytes() == content


def test_pod_snapshot_streams_with_member_and_expanded_size_caps(
    monkeypatch, tmp_path
):
    from agent_eval import kube

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        for name in ("first.txt", "second.txt"):
            member = tarfile.TarInfo(name)
            member.size = 4
            archive.addfile(member, io.BytesIO(b"data"))

    monkeypatch.setattr(
        kube,
        "_pod_archive_stream",
        lambda *args: io.BytesIO(payload.getvalue()),
    )
    monkeypatch.setattr(kube, "MAX_SNAPSHOT_MEMBERS", 1)

    with pytest.raises(UnsafeArchiveError, match="more than 1 members"):
        Pod("agent-one").copy_dir_from("/workspace", tmp_path / "member-cap")

    monkeypatch.setattr(kube, "MAX_SNAPSHOT_MEMBERS", 10)
    monkeypatch.setattr(kube, "MAX_SNAPSHOT_BYTES", 7)
    with pytest.raises(UnsafeArchiveError, match="expands beyond 7 bytes"):
        Pod("agent-one").copy_dir_from("/workspace", tmp_path / "size-cap")


def test_copy_to_non_root_volume_does_not_restore_archive_root_metadata(
    monkeypatch, tmp_path
):
    calls = []
    (tmp_path / "safe.txt").write_text("safe\n")
    monkeypatch.setattr(
        Pod,
        "exec",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, b"", b""),
    )

    def fake_run(command, timeout, *, stdin=None):
        calls.append((command, timeout, stdin.read(1)))
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("agent_eval.kube._run_bounded_command", fake_run)

    Pod("eval-one").copy_dir_to(tmp_path, "/workspace")

    command = calls[0][0]
    assert "--no-same-owner" in command
    assert "--no-same-permissions" in command
    assert "--touch" in command
    assert calls[0][1] == 300
    assert calls[0][2]


def test_copy_to_rejects_oversized_local_tree_before_pod_io(
    monkeypatch, tmp_path
):
    from agent_eval import kube

    (tmp_path / "large.bin").write_bytes(b"12345")
    monkeypatch.setattr(kube, "MAX_SNAPSHOT_BYTES", 4)
    monkeypatch.setattr(
        Pod,
        "exec",
        lambda *args, **kwargs: pytest.fail("oversized tree reached pod"),
    )

    with pytest.raises(UnsafeArchiveError, match="expands beyond 4 bytes"):
        Pod("eval-one").copy_dir_to(tmp_path, "/workspace")


def test_pod_keeps_policy_when_deletion_does_not_finish(monkeypatch):
    calls = []

    def fake_kubectl(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(args, 60)

    monkeypatch.setattr("agent_eval.kube.kubectl", fake_kubectl)

    with pytest.raises(subprocess.TimeoutExpired):
        Pod("agent-one", network_policy_name="egress-agent-one").delete()

    assert calls == [
        ("delete", "pod", "agent-one", "--ignore-not-found", "--wait=true")
    ]


def test_secret_delete_raises_on_kubectl_failure(monkeypatch):
    def fake_kubectl(*args, **kwargs):
        raise KubeError("API unavailable")

    monkeypatch.setattr("agent_eval.kube.kubectl", fake_kubectl)

    with pytest.raises(KubeError, match="API unavailable"):
        TrialSecret("trial-secret").delete()


def test_pod_snapshot_stream_is_size_bounded(monkeypatch):
    from agent_eval import kube

    class Process:
        def __init__(self, stdout):
            stdout.write(b"12345")
            stdout.flush()
            self.returncode = None

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(kube, "MAX_SNAPSHOT_BYTES", 4)
    monkeypatch.setattr(
        kube.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(kwargs["stdout"]),
    )

    with pytest.raises(UnsafeArchiveError, match="exceeds 4 bytes"):
        kube._pod_archive_stream("agent-one", "/workspace")


def test_pod_snapshot_rechecks_size_after_process_exit(monkeypatch):
    from agent_eval import kube

    class Process:
        def __init__(self, stdout):
            stdout.write(b"12345")
            stdout.flush()
            self.returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(kube, "MAX_SNAPSHOT_BYTES", 4)
    monkeypatch.setattr(
        kube.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(kwargs["stdout"]),
    )

    with pytest.raises(UnsafeArchiveError, match="exceeds 4 bytes"):
        kube._pod_archive_stream("agent-one", "/workspace")


def test_pod_snapshot_caps_stderr_even_when_process_exits_immediately(monkeypatch):
    from agent_eval import kube

    class Process:
        def __init__(self, stdout, stderr):
            del stdout
            stderr.write(b"12345")
            stderr.flush()
            self.returncode = 1

        def poll(self):
            return self.returncode

    monkeypatch.setattr(kube, "MAX_ARCHIVE_STDERR_BYTES", 4)
    monkeypatch.setattr(
        kube.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(kwargs["stdout"], kwargs["stderr"]),
    )

    with pytest.raises(KubeError, match="stderr exceeds 4 bytes"):
        kube._pod_archive_stream("agent-one", "/workspace")


def test_pod_exec_caps_output_even_when_process_exits_immediately(monkeypatch):
    from agent_eval import kube

    class Process:
        def __init__(self, stdout, stderr):
            stdout.write(b"12345")
            stderr.write(b"67890")
            stdout.flush()
            stderr.flush()
            self.returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(kube, "MAX_COMMAND_OUTPUT_BYTES", 8)
    monkeypatch.setattr(
        kube.subprocess,
        "Popen",
        lambda *args, **kwargs: Process(kwargs["stdout"], kwargs["stderr"]),
    )

    with pytest.raises(CommandOutputLimitError) as caught:
        Pod("agent-one").exec("produce-output")

    assert len(caught.value.stdout) + len(caught.value.stderr) == 8


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


def test_load_task_rejects_unknown_top_level_fields(tmp_path):
    task_dir = tmp_path / "typo"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        "id: typo\n"
        "prompt: Test task\n"
        "test_command: pytest\n"
        "resouces:\n"
        "  agent:\n"
        "    limits:\n"
        "      memory: 9Gi\n"
    )

    with pytest.raises(ValidationError, match="resouces"):
        load_task("typo", tmp_path)


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
