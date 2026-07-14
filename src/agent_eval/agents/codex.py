"""OpenAI Codex CLI adapter. Runs `codex exec` headless in the sandbox pod and
parses its JSONL event stream. Auth is file-based and projected from the
runner's unique per-trial Kubernetes Secret."""

from __future__ import annotations

import shlex
from pathlib import Path

import json

from ..kube import CREDENTIAL_MOUNT, Pod
from ..metrics import AgentMetrics
from .base import PROMPT_PATH

# item types that represent the agent acting on the environment
_TOOL_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call",
                    "patch_apply", "web_search"}


class CodexAdapter:
    name = "codex"
    env: dict[str, str] = {}

    def prepare(self, pod: Pod) -> None:
        """Stage the read-only projected credential into the writable home."""
        source = f"{CREDENTIAL_MOUNT}/codex-auth.json"
        proc = pod.exec(
            f"test -f {source} && mkdir -p $HOME/.codex && "
            f"cp {source} $HOME/.codex/auth.json && chmod 600 $HOME/.codex/auth.json",
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError("projected Codex credential is unavailable in the pod")

    def build_command(self, model: str | None = None) -> str:
        cmd = (f'codex exec --json --skip-git-repo-check '
               f"--dangerously-bypass-approvals-and-sandbox "
               f'-C /workspace "$(cat {PROMPT_PATH})"')
        if model:
            cmd = cmd.replace("codex exec", f"codex exec -m {shlex.quote(model)}", 1)
        return cmd

    def parse_transcript(self, transcript: Path) -> AgentMetrics:
        metrics = AgentMetrics()
        if not transcript.is_file():
            return metrics
        tokens_in = tokens_out = turns = tool_calls = 0
        saw_usage = False
        for line in transcript.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "turn.completed":
                turns += 1
                usage = event.get("usage") or {}
                tokens_in += usage.get("input_tokens") or 0
                tokens_out += usage.get("output_tokens") or 0
                saw_usage = True
            elif etype in ("item.completed", "item.started"):
                item = event.get("item") or {}
                if etype == "item.completed" and item.get("type") in _TOOL_ITEM_TYPES:
                    tool_calls += 1
            if metrics.model is None:
                model = event.get("model") or (event.get("item") or {}).get("model")
                if isinstance(model, str):
                    metrics.model = model
        metrics.turns = turns or None
        metrics.tool_calls = tool_calls
        if saw_usage:
            metrics.tokens_in = tokens_in
            metrics.tokens_out = tokens_out
        # cost stays None: subscription usage has no per-request price
        return metrics
