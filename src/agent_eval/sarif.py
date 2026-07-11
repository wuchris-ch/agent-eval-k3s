"""SARIF 2.1.0 serialization for change-review reports.

The serializer deliberately emits repository-relative artifact URIs.  A report
can contain paths copied from scanner output, including absolute paths from a
temporary checkout; paths that cannot be related safely to the reviewed
repository are therefore not emitted as locations.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import quote, unquote, urlsplit

if TYPE_CHECKING:
    from .review import ChangeReport


SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/schemas/"
    "sarif-schema-2.1.0.json"
)
_FINGERPRINT_NAME = "agentEvalFinding/v1"
_LEVEL_RANK = {"none": 0, "note": 1, "warning": 2, "error": 3}
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _semantic_text(value: Any) -> str:
    """Normalize human text without making it case-insensitive."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _sha256(*parts: Any) -> str:
    payload = json.dumps(
        list(parts), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slug(value: Any, *, fallback: str, limit: int = 48) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", _semantic_text(value)).strip("-.")
    return (slug or fallback)[:limit]


def _rule_id(namespace: str, label: str, *semantic_parts: Any) -> str:
    digest = _sha256("rule", namespace, *semantic_parts)[:16].upper()
    return f"AE.{namespace.upper()}.{_slug(label, fallback='finding')}.{digest}"


def _safe_relative_path(value: Any) -> str | None:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    if (
        not raw
        or raw.startswith(("/", "~"))
        or _WINDOWS_ABSOLUTE.match(raw)
        or _URI_SCHEME.match(raw)
    ):
        return None

    parts = PurePosixPath(raw).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


def _known_relative_paths(report: ChangeReport) -> tuple[str, ...]:
    paths: set[str] = set()
    for changed in report.files:
        path = _safe_relative_path(changed.path)
        if path:
            paths.add(path)
    return tuple(sorted(paths, key=lambda item: (-len(item), item)))


def _matching_known_suffix(raw_path: str, known_paths: Iterable[str]) -> str | None:
    normalized = raw_path.replace("\\", "/").rstrip("/")
    for known in known_paths:
        if normalized == known or normalized.endswith(f"/{known}"):
            return known
    return None


def _repo_relative_path(
    value: Any, repo: str, known_paths: Iterable[str]
) -> str | None:
    """Return a safe repository-relative path, or ``None`` when uncertain."""
    raw = str(value or "").strip()
    relative = _safe_relative_path(raw)
    if relative:
        return relative

    if raw.lower().startswith("file://"):
        parsed = urlsplit(raw)
        if parsed.scheme.lower() != "file" or parsed.netloc not in ("", "localhost"):
            return None
        raw = unquote(parsed.path)

    raw_normalized = raw.replace("\\", "/")
    repo_normalized = str(repo or "").strip().replace("\\", "/").rstrip("/")
    if repo_normalized and raw_normalized.casefold().startswith(
        f"{repo_normalized.casefold()}/"
    ):
        relative = _safe_relative_path(raw_normalized[len(repo_normalized) + 1 :])
        if relative:
            return relative

    if raw.startswith("/") and repo:
        try:
            candidate = Path(raw).resolve(strict=False)
            root = Path(repo).resolve(strict=False)
            relative = _safe_relative_path(candidate.relative_to(root).as_posix())
            if relative:
                return relative
        except (OSError, RuntimeError, ValueError):
            pass

    # Scanner paths often name a temporary checkout.  A suffix is safe only
    # when it exactly matches a path already known to the change report.
    return _matching_known_suffix(raw_normalized, known_paths)


def _artifact_uri(path: str) -> str:
    return quote(path, safe="/-._~")


def _positive_line(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _location(path: str | None, line: Any) -> list[dict[str, Any]] | None:
    if not path:
        return None
    physical: dict[str, Any] = {
        "artifactLocation": {
            "uri": _artifact_uri(path),
            "uriBaseId": "%SRCROOT%",
        }
    }
    start_line = _positive_line(line)
    if start_line is not None:
        physical["region"] = {"startLine": start_line}
    return [{"physicalLocation": physical}]


def _llm_level(severity: Any) -> str:
    normalized = _semantic_text(severity).casefold()
    if normalized in ("blocker", "major"):
        return "error"
    if normalized == "minor":
        return "warning"
    if normalized == "nit":
        return "note"
    return "warning"


def _scanner_level(severity: Any) -> str:
    if isinstance(severity, bool):
        return "note"
    if isinstance(severity, (int, float)):
        if severity >= 7:
            return "error"
        if severity >= 4:
            return "warning"
        return "note"

    normalized = _semantic_text(severity).casefold()
    try:
        numeric = float(normalized)
    except ValueError:
        numeric = None
    if numeric is not None:
        return _scanner_level(numeric)

    if normalized in ("critical", "high", "error", "blocker", "major"):
        return "error"
    if normalized in ("medium", "moderate", "warning", "warn", "minor"):
        return "warning"
    # Unknown and absent severities remain visible without overstating impact.
    return "note"


def _scanner_message(finding: dict[str, Any], tool: str, rule: str) -> str:
    for key in ("message", "description", "title"):
        message = _semantic_text(finding.get(key))
        if message:
            return message
    extra = finding.get("extra")
    if isinstance(extra, dict):
        message = _semantic_text(extra.get("message"))
        if message:
            return message
    return f"{tool} reported rule {rule}."


def _register_rule(
    rules: dict[str, dict[str, Any]],
    *,
    rule_id: str,
    name: str,
    description: str,
    level: str,
    properties: dict[str, Any],
) -> None:
    existing = rules.get(rule_id)
    if existing is not None:
        current = existing["defaultConfiguration"]["level"]
        if _LEVEL_RANK[level] > _LEVEL_RANK[current]:
            existing["defaultConfiguration"]["level"] = level
        return
    rules[rule_id] = {
        "id": rule_id,
        "name": name,
        "shortDescription": {"text": description},
        "defaultConfiguration": {"level": level},
        "properties": properties,
    }


def _active_llm_results(
    report: ChangeReport,
    known_paths: tuple[str, ...],
    rules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if report.llm is None:
        return []

    results: list[dict[str, Any]] = []
    for finding in report.llm.findings:
        verdict = _semantic_text(finding.verdict).casefold()
        if not finding.verified or verdict == "rejected":
            continue

        severity = _semantic_text(finding.severity).casefold() or "unknown"
        category = _semantic_text(finding.category).casefold() or "general"
        level = _llm_level(severity)
        rule_id = _rule_id(
            "LLM",
            f"{category}-{severity}",
            category,
            severity,
        )
        _register_rule(
            rules,
            rule_id=rule_id,
            name=f"LLM {category} {severity}",
            description=f"Verified {severity} {category} finding",
            level=level,
            properties={
                "source": "llm",
                "category": category,
                "severity": severity,
            },
        )

        path = _repo_relative_path(finding.file, report.repo, known_paths)
        claim = _semantic_text(finding.claim) or f"Verified {category} issue."
        evidence = _semantic_text(finding.evidence)
        semantic_anchor = evidence or claim
        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": level,
            "message": {"text": claim},
            "partialFingerprints": {
                _FINGERPRINT_NAME: _sha256(
                    "llm", category, path or "", semantic_anchor
                )
            },
            "properties": {
                "source": "llm",
                "category": category,
                "severity": severity,
                "verified": True,
            },
        }
        if evidence:
            result["properties"]["evidence"] = evidence
        if finding.verdict:
            result["properties"]["verdict"] = _semantic_text(finding.verdict)
        if finding.verdict_reason:
            result["properties"]["verdictReason"] = _semantic_text(
                finding.verdict_reason
            )
        locations = _location(path, finding.line)
        if locations:
            result["locations"] = locations
        results.append(result)
    return results


def _scanner_results(
    report: ChangeReport,
    known_paths: tuple[str, ...],
    rules: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if report.scans is None:
        return []

    results: list[dict[str, Any]] = []
    for finding in report.scans.findings:
        if not isinstance(finding, dict):
            continue
        rule = _semantic_text(finding.get("rule"))
        raw_path = finding.get("path")
        if not rule or not _semantic_text(raw_path):
            continue
        path = _repo_relative_path(raw_path, report.repo, known_paths)
        if not path:
            continue

        tool = _semantic_text(finding.get("tool")) or "scanner"
        level = _scanner_level(finding.get("severity"))
        message = _scanner_message(finding, tool, rule)
        line = _positive_line(finding.get("line"))
        rule_id = _rule_id("SCANNER", f"{tool}-{rule}", tool.casefold(), rule)
        _register_rule(
            rules,
            rule_id=rule_id,
            name=f"{tool} {rule}",
            description=f"{tool} scanner rule {rule}",
            level=level,
            properties={"source": "scanner", "tool": tool, "rule": rule},
        )

        result: dict[str, Any] = {
            "ruleId": rule_id,
            "level": level,
            "message": {"text": message},
            "partialFingerprints": {
                _FINGERPRINT_NAME: _sha256(
                    "scanner", tool.casefold(), rule, path, message, line or 0
                )
            },
            "locations": _location(path, line),
            "properties": {"source": "scanner", "tool": tool},
        }
        severity = finding.get("severity")
        if severity is not None:
            result["properties"]["scannerSeverity"] = _semantic_text(severity)
        results.append(result)
    return results


def to_sarif(report: ChangeReport) -> dict[str, Any]:
    """Convert a :class:`ChangeReport` into a SARIF 2.1.0 dictionary."""
    known_paths = _known_relative_paths(report)
    rules_by_id: dict[str, dict[str, Any]] = {}
    results = _active_llm_results(report, known_paths, rules_by_id)
    results.extend(_scanner_results(report, known_paths, rules_by_id))

    rules = sorted(rules_by_id.values(), key=lambda rule: rule["id"])
    rule_indexes = {rule["id"]: index for index, rule in enumerate(rules)}
    for result in results:
        result["ruleIndex"] = rule_indexes[result["ruleId"]]

    invocation = {
        "executionSuccessful": not report.blocked,
        "properties": {
            "base": report.base,
            "head": report.head,
            "overallRisk": report.risk,
            "blocked": report.blocked,
        },
    }
    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "agent-eval",
                        "semanticVersion": "0.1.0",
                        "rules": rules,
                    }
                },
                "invocations": [invocation],
                "results": results,
            }
        ],
    }


def write_sarif(report: ChangeReport, path: str | Path) -> Path:
    """Serialize ``report`` as UTF-8 SARIF JSON and return the output path."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(to_sarif(report), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output
