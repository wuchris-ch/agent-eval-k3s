import json
import subprocess

from agent_eval import cluster


def _completed(payload, returncode=0):
    stdout = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_cluster_exists_parses_compact_k3d_json(monkeypatch):
    monkeypatch.setattr(
        cluster.subprocess,
        "run",
        lambda *args, **kwargs: _completed(
            [{"name": "agent-eval", "serversCount": 1}]
        ),
    )

    assert cluster.cluster_exists()


def test_cluster_exists_fails_closed_on_malformed_k3d_json(monkeypatch):
    monkeypatch.setattr(
        cluster.subprocess,
        "run",
        lambda *args, **kwargs: _completed("not json"),
    )

    assert not cluster.cluster_exists()


def test_cluster_up_starts_an_existing_stopped_cluster(monkeypatch):
    stopped = {
        "name": "agent-eval",
        "serversCount": 1,
        "serversRunning": 0,
        "agentsCount": 1,
        "agentsRunning": 0,
    }
    commands = []
    namespace_calls = []
    monkeypatch.setattr(cluster, "_cluster_record", lambda: stopped)
    monkeypatch.setattr(cluster, "_run", lambda command: commands.append(command))
    monkeypatch.setattr(
        cluster, "ensure_namespace", lambda: namespace_calls.append(True)
    )

    cluster.cluster_up()

    assert commands == [["k3d", "cluster", "start", "agent-eval", "--wait"]]
    assert namespace_calls == [True]
