"""Parse test artifacts (junit XML, pytest-cov JSON) produced in the eval pod."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field


class TestResults(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failures: list[str] = Field(default_factory=list)
    coverage_percent: float | None = None
    # eval infrastructure problems (missing junit etc.), distinct from test failures
    infra_error: str | None = None

    @property
    def resolved(self) -> bool:
        return self.infra_error is None and self.total > 0 and self.failed == 0 and self.errors == 0


def parse_junit(junit_path: Path) -> TestResults:
    if not junit_path.is_file():
        return TestResults(infra_error=f"junit xml not produced at {junit_path.name}")
    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError as e:
        return TestResults(infra_error=f"junit xml unparseable: {e}")

    suites = root.iter("testsuite") if root.tag == "testsuites" else [root]
    results = TestResults()
    for suite in suites:
        results.total += int(suite.get("tests", 0))
        results.failed += int(suite.get("failures", 0))
        results.errors += int(suite.get("errors", 0))
        results.skipped += int(suite.get("skipped", 0))
        for case in suite.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                results.failures.append(f"{case.get('classname', '')}::{case.get('name', '')}")
    results.passed = results.total - results.failed - results.errors - results.skipped
    return results


def parse_coverage(coverage_json_path: Path) -> float | None:
    if not coverage_json_path.is_file():
        return None
    try:
        data = json.loads(coverage_json_path.read_text())
        return round(float(data["totals"]["percent_covered"]), 2)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None
