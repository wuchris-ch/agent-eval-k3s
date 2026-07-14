"""Per-trial credential material with an optional short-lived broker hook.

Secrets are kept in memory until the runner creates a uniquely named
Kubernetes Secret for one trial.  The runner deletes that Secret in a finally
block.  A broker can mint genuinely short-lived credentials; the built-in
fallbacks only provide per-trial delivery of the user's existing credential
and are deliberately labelled as reusable.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
BROKER_TIMEOUT_SECONDS = 30
SHORT_LIVED_MAX_TTL_SECONDS = 3600


@dataclass(frozen=True)
class CredentialMaterial:
    """Credential values and their safe Kubernetes projections."""

    values: dict[str, str]
    env_keys: tuple[str, ...] = ()
    file_items: dict[str, str] = field(default_factory=dict)
    source: str = "unknown"
    expires_at: str | None = None
    short_lived: bool = False

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("credential material must contain at least one value")
        unknown_env = set(self.env_keys) - set(self.values)
        unknown_files = set(self.file_items) - set(self.values)
        if unknown_env or unknown_files:
            raise ValueError("credential projections reference missing values")
        if any(not _ENV_NAME.fullmatch(key) for key in self.env_keys):
            raise ValueError("credential environment names must be valid identifiers")
        for key, path in self.file_items.items():
            if not _SECRET_KEY.fullmatch(key):
                raise ValueError(f"invalid Kubernetes Secret key {key!r}")
            pure = PurePosixPath(path)
            if pure.is_absolute() or len(pure.parts) != 1 or pure.name in ("", ".", ".."):
                raise ValueError("credential file paths must be safe basenames")

    @property
    def mode(self) -> str:
        if self.short_lived:
            return "short-lived"
        if self.expires_at:
            return "expiring-broker-credential"
        return "reusable-fallback"


def _parse_expiry(
    value: object, *, minimum_ttl_seconds: int = 0
) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if not isinstance(value, str):
        raise ValueError("credential broker expires_at must be an ISO-8601 string")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("credential broker expires_at is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("credential broker expires_at must include a timezone")
    remaining = (parsed - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise ValueError("credential broker returned an expired credential")
    if remaining < minimum_ttl_seconds:
        raise ValueError(
            "credential broker expiry does not cover the configured trial timeout"
        )
    return value, remaining <= SHORT_LIVED_MAX_TTL_SECONDS


def _from_broker(
    command: str, agent: str, *, minimum_ttl_seconds: int = 0
) -> CredentialMaterial:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("AGENT_EVAL_CREDENTIAL_COMMAND is empty")
    env = dict(os.environ)
    env["AGENT_EVAL_AGENT"] = agent
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=BROKER_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("credential broker could not be executed") from exc
    if proc.returncode != 0:
        # Broker output may contain credentials.  Never include it in errors.
        raise RuntimeError(f"credential broker exited {proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("credential broker did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("credential broker output must be a JSON object")
    allowed = {"env", "files", "expires_at"}
    extra = set(payload) - allowed
    if extra:
        raise ValueError(f"credential broker returned unsupported fields: {sorted(extra)}")
    raw_env = payload.get("env") or {}
    raw_files = payload.get("files") or {}
    if not isinstance(raw_env, dict) or not isinstance(raw_files, dict):
        raise ValueError("credential broker env and files must be JSON objects")

    values: dict[str, str] = {}
    env_keys: list[str] = []
    file_items: dict[str, str] = {}
    for key, value in raw_env.items():
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
            raise ValueError("credential broker returned an invalid environment name")
        if not isinstance(value, str) or not value:
            raise ValueError("credential broker values must be non-empty strings")
        values[key] = value
        env_keys.append(key)
    for index, (path, value) in enumerate(raw_files.items()):
        if not isinstance(path, str) or not isinstance(value, str) or not value:
            raise ValueError("credential broker files must map names to non-empty strings")
        secret_key = f"broker-file-{index}"
        values[secret_key] = value
        file_items[secret_key] = path

    expires_at, short_lived = _parse_expiry(
        payload.get("expires_at"), minimum_ttl_seconds=minimum_ttl_seconds
    )
    return CredentialMaterial(
        values=values,
        env_keys=tuple(env_keys),
        file_items=file_items,
        source="credential-broker",
        expires_at=expires_at,
        short_lived=short_lived,
    )


def load_trial_credentials(
    agent: str, *, minimum_ttl_seconds: int = 0
) -> CredentialMaterial:
    """Load only the credential needed by ``agent`` for one trial.

    Set ``AGENT_EVAL_CREDENTIAL_COMMAND`` to an argv-style command whose JSON
    stdout contains ``env``, ``files``, and an optional ``expires_at``.  When
    no broker is configured, supported adapters use the current reusable host
    credential, but it is still delivered through a unique per-trial Secret.
    """

    broker = os.environ.get("AGENT_EVAL_CREDENTIAL_COMMAND")
    if broker:
        return _from_broker(
            broker, agent, minimum_ttl_seconds=minimum_ttl_seconds
        )

    if agent == "claude-code":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set and no credential broker is configured"
            )
        return CredentialMaterial(
            values={"ANTHROPIC_API_KEY": key},
            env_keys=("ANTHROPIC_API_KEY",),
            source="host-environment",
        )

    if agent == "codex":
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            raise RuntimeError("~/.codex/auth.json not found; run `codex login` first")
        return CredentialMaterial(
            values={"codex-auth": auth.read_text(encoding="utf-8")},
            file_items={"codex-auth": "codex-auth.json"},
            source="host-codex-auth",
        )

    raise RuntimeError(
        f"agent {agent!r} needs AGENT_EVAL_CREDENTIAL_COMMAND credential material"
    )
