"""Safe YAML loading helpers shared by versioned configuration formats."""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


class DuplicateKeyError(ValueError):
    """Raised when a YAML mapping repeats a key at any nesting level."""


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader, node: MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise DuplicateKeyError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def load_unique_yaml(text: str | bytes) -> Any:
    """Safely parse YAML while rejecting duplicate keys."""

    return yaml.load(text, Loader=UniqueKeyLoader)
