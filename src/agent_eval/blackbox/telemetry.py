"""Optional content-free OTel projection, independent of target instrumentation."""

from __future__ import annotations

import os

from .. import observability
from .models import Report, Result, digest


def result_attributes(report: Report, result: Result) -> dict:
    attributes = {
        "agent_eval.telemetry.schema_version": "agent-eval.blackbox/v1",
        "agent_eval.run.id": report.run_id,
        "agent_eval.suite.sha256": report.suite_sha256,
        "agent_eval.agent.sha256": digest(report.agent),
        "agent_eval.target.sha256": report.target_sha256,
        "agent_eval.case.sha256": digest(result.case_id),
        "agent_eval.trial.number": result.trial,
        "agent_eval.trial.outcome": result.outcome,
        "agent_eval.target.mode": report.mode,
    }
    if result.score is not None:
        attributes["agent_eval.trial.score"] = result.score
    if result.error:
        attributes["agent_eval.error.type"] = result.error
    if result.observation and result.observation.latency_ms is not None:
        attributes["agent_eval.trial.latency_ms"] = result.observation.latency_ms
    return attributes


def export_report(report: Report) -> bool:
    if (
        not observability._enabled()
        or os.environ.get("OTEL_TRACES_EXPORTER", "otlp").casefold() == "none"
    ):
        return False
    try:
        from opentelemetry.trace import Link, SpanContext, TraceFlags

        runtime = observability._get_runtime()
        with runtime.tracer.start_as_current_span(
            "agent_eval.blackbox.run",
            attributes={
                "agent_eval.run.id": report.run_id,
                "agent_eval.run.passed": report.passed,
                "agent_eval.run.overall_passed": report.overall_passed,
                "agent_eval.suite.sha256": report.suite_sha256,
            },
        ):
            for result in report.results:
                observation = result.observation
                links = []
                if observation and observation.trace_id and observation.span_id:
                    links.append(
                        Link(
                            SpanContext(
                                trace_id=int(observation.trace_id, 16),
                                span_id=int(observation.span_id, 16),
                                is_remote=True,
                                trace_flags=TraceFlags(TraceFlags.SAMPLED),
                            )
                        )
                    )
                with runtime.tracer.start_as_current_span(
                    "agent_eval.blackbox.trial",
                    attributes=result_attributes(report, result),
                    links=links,
                ) as span:
                    for metric in result.metrics:
                        span.add_event(
                            "gen_ai.evaluation.result",
                            {
                                "gen_ai.evaluation.name": metric.kind,
                                "gen_ai.evaluation.score.value": metric.score,
                                "agent_eval.metric.sha256": digest(metric.name),
                                "agent_eval.metric.threshold": metric.threshold,
                                "agent_eval.metric.passed": metric.passed,
                            },
                        )
            if report.inspection is not None:
                inspection = report.inspection
                with runtime.tracer.start_as_current_span(
                    "agent_eval.inspection",
                    attributes={
                        "agent_eval.inspection.status": inspection.status,
                        "agent_eval.inspection.required": inspection.required,
                        "agent_eval.inspection.profile_sha256": inspection.profile_sha256,
                        "agent_eval.inspection.source.status": inspection.source.status,
                        "agent_eval.inspection.trace.status": inspection.trace.status,
                    },
                ) as span:
                    for result in inspection.results:
                        attributes = {
                            "agent_eval.inspection.check_sha256": digest(result.name),
                            "agent_eval.inspection.kind": result.kind,
                            "agent_eval.inspection.scope": result.scope,
                            "agent_eval.inspection.status": result.status,
                        }
                        if result.score is not None:
                            attributes["agent_eval.inspection.score"] = result.score
                        if result.error is not None:
                            attributes["agent_eval.inspection.error.type"] = (
                                result.error
                            )
                        if result.case_id is not None:
                            attributes["agent_eval.case.sha256"] = digest(
                                result.case_id
                            )
                            attributes["agent_eval.trial.number"] = result.trial
                        span.add_event("agent_eval.inspection.result", attributes)
        return bool(runtime.provider.force_flush(observability._flush_timeout_ms()))
    except Exception:
        return False
