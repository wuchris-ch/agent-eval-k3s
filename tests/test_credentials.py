from datetime import datetime, timedelta, timezone

import pytest

from agent_eval.credentials import CredentialMaterial, load_trial_credentials


def test_adapter_credentials_are_scoped(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-key")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens": "codex-test-token"}')

    claude = load_trial_credentials("claude-code")
    codex = load_trial_credentials("codex")

    assert claude.env_keys == ("ANTHROPIC_API_KEY",)
    assert "codex-auth" not in claude.values
    assert codex.env_keys == ()
    assert codex.file_items == {"codex-auth": "codex-auth.json"}
    assert "ANTHROPIC_API_KEY" not in codex.values


def test_broker_material_is_short_lived_and_never_uses_shell(monkeypatch, tmp_path):
    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    broker = tmp_path / "broker.py"
    broker.write_text(
        "import json\n"
        f"print(json.dumps({{'env': {{'TOKEN': 'value'}}, 'expires_at': {expires!r}}}))\n"
    )
    monkeypatch.setenv(
        "AGENT_EVAL_CREDENTIAL_COMMAND", f"python {broker} --literal-semicolon ';'"
    )

    material = load_trial_credentials("custom")

    assert material.values == {"TOKEN": "value"}
    assert material.mode == "short-lived"
    assert material.source == "credential-broker"


def test_broker_expiry_must_cover_trial_and_long_ttl_is_not_short_lived(
    monkeypatch, tmp_path
):
    broker = tmp_path / "broker.py"
    expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    broker.write_text(
        "import json\n"
        f"print(json.dumps({{'env': {{'TOKEN': 'value'}}, 'expires_at': {expires!r}}}))\n"
    )
    monkeypatch.setenv("AGENT_EVAL_CREDENTIAL_COMMAND", f"python {broker}")

    with pytest.raises(ValueError, match="trial timeout"):
        load_trial_credentials("custom", minimum_ttl_seconds=300)

    far_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    broker.write_text(
        "import json\n"
        f"print(json.dumps({{'env': {{'TOKEN': 'value'}}, 'expires_at': {far_expiry!r}}}))\n"
    )
    material = load_trial_credentials("custom", minimum_ttl_seconds=300)
    assert material.mode == "expiring-broker-credential"


def test_broker_failure_does_not_echo_secret_output(monkeypatch, tmp_path):
    broker = tmp_path / "bad.py"
    broker.write_text("import sys\nprint('VERY_SECRET')\nsys.exit(4)\n")
    monkeypatch.setenv("AGENT_EVAL_CREDENTIAL_COMMAND", f"python {broker}")

    with pytest.raises(RuntimeError) as caught:
        load_trial_credentials("custom")

    assert "VERY_SECRET" not in str(caught.value)


@pytest.mark.parametrize(
    "material",
    [
        lambda: CredentialMaterial(values={}),
        lambda: CredentialMaterial(values={"x": "y"}, env_keys=("BAD-NAME",)),
        lambda: CredentialMaterial(
            values={"x": "y"}, file_items={"x": "../auth.json"}
        ),
    ],
)
def test_invalid_material_is_rejected(material):
    with pytest.raises(ValueError):
        material()


def test_broker_rejects_unknown_fields_without_exposing_values(monkeypatch, tmp_path):
    broker = tmp_path / "bad-schema.py"
    broker.write_text(
        "import json\n"
        "print(json.dumps({'env': {'TOKEN': 'VERY_SECRET'}, 'unexpected': True}))\n"
    )
    monkeypatch.setenv("AGENT_EVAL_CREDENTIAL_COMMAND", f"python {broker}")

    with pytest.raises(ValueError) as caught:
        load_trial_credentials("custom")

    assert "unexpected" in str(caught.value)
    assert "VERY_SECRET" not in str(caught.value)
