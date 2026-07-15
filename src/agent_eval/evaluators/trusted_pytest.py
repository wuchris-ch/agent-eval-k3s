"""Trusted pytest bootstrap copied into the evaluator's hidden-test volume.

Pytest and its installed plugins are imported before the submitted workspace
is added to ``sys.path``. This prevents a top-level ``pytest.py`` or plugin
lookalike in agent output from replacing the test runner during startup.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys


def main() -> int:
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest

    plugins = []
    for entry_point in importlib.metadata.entry_points(group="pytest11"):
        plugins.append(entry_point.load())

    if os.environ.get("AGENT_EVAL_EVALUATION_MODE") == "isolated-black-box":
        os.chdir("/tests")
    else:
        os.chdir("/workspace")
        sys.path.insert(0, "/workspace")
    return pytest.main(sys.argv[1:], plugins=plugins)


if __name__ == "__main__":
    raise SystemExit(main())
