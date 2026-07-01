"""Agent adapters: how to invoke a coding agent inside the sandbox pod and how
to parse its transcript into efficiency metrics."""

from __future__ import annotations

from .base import PROMPT_PATH, AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter

_ADAPTERS: dict[str, AgentAdapter] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter(),
    CodexAdapter.name: CodexAdapter(),
}


def get_adapter(name: str) -> AgentAdapter:
    if name not in _ADAPTERS:
        raise KeyError(f"unknown agent {name!r}; available: {', '.join(_ADAPTERS)}")
    return _ADAPTERS[name]


__all__ = ["AgentAdapter", "get_adapter", "PROMPT_PATH"]
