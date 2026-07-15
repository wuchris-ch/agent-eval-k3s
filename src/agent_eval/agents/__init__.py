"""Agent adapters: how to invoke a coding agent inside the sandbox pod and how
to parse its transcript into efficiency metrics."""

from __future__ import annotations

from .base import PROMPT_PATH, AgentAdapter
from .registry import (
    ENTRY_POINT_GROUP,
    AdapterMetadata,
    BUILTIN_ADAPTER_NAMES,
    get_adapter,
    is_builtin_adapter,
    list_adapters,
)


__all__ = [
    "AdapterMetadata",
    "AgentAdapter",
    "BUILTIN_ADAPTER_NAMES",
    "ENTRY_POINT_GROUP",
    "get_adapter",
    "is_builtin_adapter",
    "list_adapters",
    "PROMPT_PATH",
]
