"""Private run artifacts and read-only observation import from files or databases."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inspection import InspectionEvidence

from ..limits import MAX_RESULTS_JSON_BYTES, read_stable_bounded_file
from ..paths import (
    atomic_write_private,
    ensure_private_directory,
    ensure_private_file,
    get_state_dir,
)
from .models import (
    MAX_EVALUATIONS,
    Observation,
    Report,
    Suite,
    digest,
    json_bytes,
    parse_json,
)


def load_observations(path: Path) -> list[Observation]:
    raw = read_stable_bounded_file(path, maximum_bytes=MAX_RESULTS_JSON_BYTES)
    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > MAX_EVALUATIONS:
        raise ValueError("too many observations")
    return [Observation.model_validate(parse_json(line)) for line in lines]


def _read_rows(cursor) -> list[Observation]:
    names = [column[0] for column in cursor.description or []]
    if len(names) != len(set(names)) or not {
        "case_id",
        "input_sha256",
        "actual_output_json",
    } <= set(names):
        raise ValueError(
            "query must return unique case_id, input_sha256, actual_output_json columns"
        )
    rows = []
    size = 0
    while row := cursor.fetchone():
        if len(rows) >= MAX_EVALUATIONS:
            raise ValueError("query returned too many observations")
        item = dict(zip(names, row, strict=True))
        encoded = item.pop("actual_output_json")
        if not isinstance(encoded, (str, bytes)):
            raise ValueError(
                "actual_output_json must be JSON text; cast JSON/JSONB to text"
            )
        size += len(encoded.encode("utf-8") if isinstance(encoded, str) else encoded)
        if size > MAX_RESULTS_JSON_BYTES:
            raise ValueError("query results exceed the byte limit")
        item["actual_output"] = parse_json(encoded)
        # Optional SQL NULLs mean unavailable; model defaults handle trial/status.
        for key in ("trial", "status"):
            if item.get(key, "present") is None:
                del item[key]
        rows.append(Observation.model_validate(item))
    return rows


def load_database_observations(
    query_path: Path,
    *,
    sqlite_path: Path | None = None,
    postgres_dsn: str | None = None,
) -> list[Observation]:
    if (sqlite_path is None) == (postgres_dsn is None):
        raise ValueError("select exactly one database source")
    query = read_stable_bounded_file(query_path, maximum_bytes=64 * 1024).decode(
        "utf-8"
    )
    if sqlite_path is not None:
        # URI mode=ro and the authorizer also block ATTACH and write pragmas.
        connection = sqlite3.connect(
            sqlite_path.resolve().as_uri() + "?mode=ro", uri=True
        )
        deadline = time.monotonic() + 30
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

        def authorize(action, arg1, arg2, _database, _source):
            allowed = {
                sqlite3.SQLITE_SELECT,
                sqlite3.SQLITE_READ,
                sqlite3.SQLITE_RECURSIVE,
            }
            if action == sqlite3.SQLITE_FUNCTION:
                return (
                    sqlite3.SQLITE_DENY
                    if (arg2 or arg1 or "").lower() == "load_extension"
                    else sqlite3.SQLITE_OK
                )
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        try:
            return _read_rows(connection.execute(query))
        finally:
            connection.close()
    try:
        import psycopg
    except ImportError:
        raise RuntimeError(
            "install the database extra to read Postgres observations"
        ) from None
    # Prepared extended-query execution rejects multi-statement SQL. Use a
    # read-only database account too, especially with operator-authored functions.
    with psycopg.connect(postgres_dsn, connect_timeout=10) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        # Bound rows on the server as well as during import. A subquery also
        # restricts this interface to a single SELECT/CTE result set.
        select = query.strip().removesuffix(";")
        bounded_query = (
            f"SELECT * FROM ({select}) AS eval_observations LIMIT {MAX_EVALUATIONS + 1}"
        )
        return _read_rows(connection.execute(bounded_query, prepare=True))


def save_report(
    suite: Suite, report: Report, *, evidence: InspectionEvidence | None = None
) -> Path:
    if digest(suite.model_dump(mode="json")) != report.suite_sha256:
        raise ValueError("suite no longer matches the evaluated snapshot")
    if report.inspection is not None:
        if evidence is not None and evidence.source is not None:
            type(evidence.source).model_validate(
                evidence.source.model_dump(mode="json")
            )
        if evidence is None or (
            digest(evidence.profile.model_dump(mode="json"))
            != report.inspection.profile_sha256
            or (evidence.source.sha256 if evidence.source else None)
            != report.inspection.source_sha256
            or evidence.traces_sha256 != report.inspection.traces_sha256
        ):
            raise ValueError("inspection evidence does not match the report")
    root = ensure_private_directory(get_state_dir() / "blackbox", parents=True)
    run_dir = ensure_private_directory(root / report.run_id, exist_ok=False)
    atomic_write_private(
        run_dir / "suite.json", json_bytes(suite.model_dump(mode="json")) + b"\n"
    )
    path = run_dir / "report.json"
    atomic_write_private(path, report.model_dump_json(indent=2).encode("utf-8") + b"\n")
    observations = [
        result.observation.model_dump_json()
        for result in report.results
        if result.observation
    ]
    atomic_write_private(
        run_dir / "observations.jsonl", ("\n".join(observations) + "\n").encode("utf-8")
    )
    if report.inspection is not None:
        atomic_write_private(
            run_dir / "inspection-profile.json",
            json_bytes(evidence.profile.model_dump(mode="json")) + b"\n",
        )
        if evidence.source is not None:
            atomic_write_private(
                run_dir / "source.json",
                json_bytes(evidence.source.model_dump(mode="json")) + b"\n",
            )
        if evidence.traces is not None:
            atomic_write_private(
                run_dir / "traces.jsonl",
                b"\n".join(
                    json_bytes(record.model_dump(mode="json"))
                    for record in evidence.traces
                )
                + b"\n",
            )
    # JSON is authoritative. A local index supports querying across agents/runs.
    database = ensure_private_file(root / "metrics.db")
    with sqlite3.connect(database) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS blackbox_runs (
                run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, agent TEXT NOT NULL,
                suite_sha256 TEXT NOT NULL, mode TEXT NOT NULL, evaluations INTEGER NOT NULL,
                accepted INTEGER NOT NULL, rejected INTEGER NOT NULL, infra_errors INTEGER NOT NULL,
                average_score REAL NOT NULL, report_path TEXT NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO blackbox_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report.run_id,
                report.created_at,
                report.agent,
                report.suite_sha256,
                report.mode,
                report.evaluations,
                report.accepted,
                report.rejected,
                report.infra_errors,
                report.average_score,
                str(path),
            ),
        )
        if report.inspection is not None:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS inspection_runs (
                    run_id TEXT PRIMARY KEY, profile_sha256 TEXT NOT NULL,
                    source_sha256 TEXT, traces_sha256 TEXT, required INTEGER NOT NULL,
                    status TEXT NOT NULL, source_status TEXT NOT NULL, trace_status TEXT NOT NULL,
                    source_score REAL, trace_score REAL, overall_passed INTEGER NOT NULL
                )
            """)
            item = report.inspection
            connection.execute(
                "INSERT INTO inspection_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report.run_id,
                    item.profile_sha256,
                    item.source_sha256,
                    item.traces_sha256,
                    item.required,
                    item.status,
                    item.source.status,
                    item.trace.status,
                    item.source.score,
                    item.trace.score,
                    report.overall_passed,
                ),
            )
    return path
