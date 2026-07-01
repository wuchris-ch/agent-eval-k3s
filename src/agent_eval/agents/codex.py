"""OpenAI Codex CLI adapter. Runs `codex exec` headless in the sandbox pod and
parses its JSONL event stream. Auth is file-based (ChatGPT subscription or API
key in ~/.codex/auth.json), so prepare() copies the host credential into the
pod rather than using the k8s env secret."""

from __future__ import annotations

import shlex
import shutil
import tempfile
from pathlib import Path

import json

from ..kube import Pod
from ..metrics import AgentMetrics
from .base import PROMPT_PATH

# item types that represent the agent acting on the environment
_TOOL_ITEM_TYPES = {"command_execution", "file_change", "mcp_tool_call",
                    "patch_apply", "web_search"}


class CodexAdapter:
    name = "codex"
    env: dict[str, str] = {}

    def prepare(self, pod: Pod) -> None:
        """Copy the host's codex credential into the pod (subscription auth
        lives in ~/.codex/auth.json and cannot be passed as an env var)."""
        auth = Path.home() / ".codex" / "auth.json"
        if not auth.is_file():
            raise RuntimeError("~/.codex/auth.json not found; run `codex login` first")
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(auth, Path(tmp) / "auth.json")
            pod.copy_dir_to(Path(tmp), "/root/.codex")

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
