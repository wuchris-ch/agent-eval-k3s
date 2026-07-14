"""Parse test artifacts (junit XML, pytest-cov JSON) produced in the eval pod."""

from __future__ import annotations

import json
import math
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
    # The artifact alone is not sufficient evidence of success. The command
    # that produced it must also have completed successfully.
    command_exit_code: int | None = None
    # A hostile or structurally unsafe submission is a rejected result, not an
    # infrastructure outage.
    integrity_error: str | None = None
    runtime_image_digest: str | None = None
    # eval infrastructure problems (missing junit etc.), distinct from test failures
    infra_error: str | None = None

    @property
    def resolved(self) -> bool:
        return (
            self.infra_error is None
            and self.integrity_error is None
            and self.command_exit_code == 0
            and self.total > 0
            and self.passed > 0
            and self.failed == 0
            and self.errors == 0
        )


def parse_junit(
    junit_path: Path, *, command_exit_code: int | None = None
) -> TestResults:
    def failed(reason: str) -> TestResults:
        return TestResults(
            command_exit_code=command_exit_code,
            infra_error=reason,
        )

    try:
        exists = junit_path.is_file()
    except OSError as exc:
        return failed(f"junit xml unreadable: {type(exc).__name__}")
    if not exists:
        return failed(f"junit xml not produced at {junit_path.name}")

    try:
        root = ET.parse(junit_path).getroot()
    except ET.ParseError as exc:
        return failed(f"junit xml unparseable: {exc}")
    except (LookupError, OSError, UnicodeError, ValueError) as exc:
        return failed(f"junit xml unreadable: {type(exc).__name__}")

    def local_name(tag: object) -> str:
        if not isinstance(tag, str):
            return ""
        return tag.rsplit("}", 1)[-1]

    root_name = local_name(root.tag)
    if root_name == "testsuite":
        suites = [root]
    elif root_name == "testsuites":
        suites = [child for child in root if local_name(child.tag) == "testsuite"]
        if not suites:
            return failed("junit xml invalid: <testsuites> contains no test suites")
    else:
        return failed(
            "junit xml invalid: root must be <testsuite> or <testsuites>"
        )

    def count(element: ET.Element, field: str, *, required: bool = False) -> int:
        raw = element.get(field)
        if raw is None:
            if required:
                raise ValueError(f"missing {field!r} count")
            return 0
        if not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"{field!r} count must be a non-negative integer")
        return int(raw)

    results = TestResults(command_exit_code=command_exit_code)
    try:
        for suite in suites:
            total = count(suite, "tests", required=True)
            failed_count = count(suite, "failures")
            error_count = count(suite, "errors")
            skipped_count = count(suite, "skipped")
            if failed_count + error_count + skipped_count > total:
                raise ValueError(
                    "failures, errors, and skipped counts exceed tests count"
                )

            results.total += total
            results.failed += failed_count
            results.errors += error_count
            results.skipped += skipped_count
            for case in suite.iter():
                if local_name(case.tag) != "testcase":
                    continue
                child_names = {local_name(child.tag) for child in case}
                if "failure" in child_names or "error" in child_names:
                    results.failures.append(
                        f"{case.get('classname', '')}::{case.get('name', '')}"
                    )

        if root_name == "testsuites":
            aggregate = {
                "tests": results.total,
                "failures": results.failed,
                "errors": results.errors,
                "skipped": results.skipped,
            }
            for field, actual in aggregate.items():
                if root.get(field) is not None and count(root, field) != actual:
                    raise ValueError(
                        f"aggregate {field!r} count does not match test suites"
                    )
    except ValueError as exc:
        return failed(f"junit xml invalid: {exc}")

    results.passed = results.total - results.failed - results.errors - results.skipped
    return results


def parse_coverage(coverage_json_path: Path) -> float | None:
    try:
        exists = coverage_json_path.is_file()
    except OSError:
        return None
    if not exists:
        return None
    try:
        data = json.loads(coverage_json_path.read_text())
        value = data["totals"]["percent_covered"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        percent = float(value)
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            return None
        return round(percent, 2)
    except (
        json.JSONDecodeError,
        KeyError,
        OSError,
        OverflowError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
