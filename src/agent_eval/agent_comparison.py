"""Cross-run coding-agent summaries with explicit denominators and pairing."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Callable

from pydantic import BaseModel, Field

from .metrics import RunRecord
from .report import pass_at_k

_Z_95 = 1.959963984540054


class Interval(BaseModel):
    lower: float
    upper: float
    confidence: float = 0.95


class Distribution(BaseModel):
    observed: int
    total: int
    completeness: float
    median: float | None = None
    p95: float | None = None
    mean: float | None = None


class AgentSummary(BaseModel):
    agent: str
    model: str
    sample_size: int
    sample_label: str
    correctness_evidence_count: int
    legacy_incomplete_count: int
    resolved: int
    resolved_rate: float | None
    resolved_wilson_95: Interval | None
    accepted: int | None = None
    accepted_evidence_count: int = 0
    accepted_rate: float | None = None
    infrastructure_failures: int = 0
    infrastructure_failure_rate: float = 0.0
    pass_at_k: dict[str, float] = Field(default_factory=dict)
    wall_time_s: Distribution
    total_tokens: Distribution
    cost_usd: Distribution
    tool_calls: Distribution
    judge_score: Distribution
    changed_lines: Distribution


class PairedDelta(BaseModel):
    baseline: str
    candidate: str
    pairs: int
    candidate_wins: int
    ties: int
    candidate_losses: int
    duplicate_keys_excluded: int = 0
    resolved_rate_delta: float | None
    wall_time_median_delta_s: float | None
    token_median_delta: float | None
    cost_median_delta_usd: float | None


class AgentComparison(BaseModel):
    summaries: list[AgentSummary]
    paired: list[PairedDelta] = Field(default_factory=list)


def _wilson(successes: int, total: int) -> Interval:
    if total <= 0:
        return Interval(lower=0.0, upper=0.0)
    proportion = successes / total
    z2 = _Z_95**2
    denominator = 1 + z2 / total
    center = (proportion + z2 / (2 * total)) / denominator
    margin = _Z_95 * math.sqrt(
        proportion * (1 - proportion) / total + z2 / (4 * total**2)
    ) / denominator
    return Interval(
        lower=max(0.0, center - margin),
        upper=min(1.0, center + margin),
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def _distribution(records: list[RunRecord], getter: Callable[[RunRecord], float | int | None]) -> Distribution:
    values = [float(value) for record in records if (value := getter(record)) is not None]
    total = len(records)
    return Distribution(
        observed=len(values),
        total=total,
        completeness=len(values) / total if total else 0.0,
        median=statistics.median(values) if values else None,
        p95=_percentile(values, 0.95),
        mean=statistics.fmean(values) if values else None,
    )


def _model(record: RunRecord) -> str:
    observed = record.efficiency.model
    requested = record.efficiency.requested_model
    if observed and requested and observed != requested:
        return f"{observed} (requested {requested})"
    if observed:
        return observed
    if requested:
        return f"{requested} (requested, unobserved)"
    return "unknown"


def _agent_key(record: RunRecord) -> str:
    return f"{record.agent}/{_model(record)}"


def _total_tokens(record: RunRecord) -> int | None:
    if record.efficiency.tokens_in is None or record.efficiency.tokens_out is None:
        return None
    return record.efficiency.tokens_in + record.efficiency.tokens_out


def _accepted(record: RunRecord) -> bool | None:
    outcome = getattr(record, "outcome", None)
    return None if outcome is None else outcome.status == "accepted"


def _summarize(records: list[RunRecord]) -> AgentSummary:
    first = records[0]
    total = len(records)
    evaluable = [
        record for record in records
        if record.correctness.command_exit_code is not None
        or record.correctness.infra_error is not None
    ]
    resolved = sum(record.correctness.resolved for record in evaluable)
    accepted_values = [_accepted(record) for record in records]
    accepted_observed = [value for value in accepted_values if value is not None]
    infra = sum(
        bool(record.correctness.infra_error or record.efficiency.infra_error)
        for record in records
    )
    return AgentSummary(
        agent=first.agent,
        model=_model(first),
        sample_size=total,
        sample_label="small sample" if total < 10 else "established sample",
        correctness_evidence_count=len(evaluable),
        legacy_incomplete_count=total - len(evaluable),
        resolved=resolved,
        resolved_rate=resolved / len(evaluable) if evaluable else None,
        resolved_wilson_95=(
            _wilson(resolved, len(evaluable)) if evaluable else None
        ),
        accepted=(sum(accepted_observed) if accepted_observed else None),
        accepted_evidence_count=len(accepted_observed),
        accepted_rate=(
            sum(accepted_observed) / len(accepted_observed)
            if accepted_observed else None
        ),
        infrastructure_failures=infra,
        infrastructure_failure_rate=infra / total,
        pass_at_k={
            f"pass@{k}": pass_at_k(len(evaluable), resolved, k)
            for k in (1, 3, 5)
            if k <= len(evaluable)
        },
        wall_time_s=_distribution(records, lambda r: r.efficiency.wall_time_s),
        total_tokens=_distribution(records, _total_tokens),
        cost_usd=_distribution(records, lambda r: r.efficiency.cost_usd),
        tool_calls=_distribution(records, lambda r: r.efficiency.tool_calls),
        judge_score=_distribution(records, lambda r: r.judge.weighted_score),
        changed_lines=_distribution(
            records, lambda r: r.diff.lines_added + r.diff.lines_removed
        ),
    )


def _pair_key(record: RunRecord) -> tuple[str, str, int, str, str] | None:
    experiment_id = getattr(record, "experiment_id", None)
    has_evidence = (
        record.correctness.command_exit_code is not None
        or record.correctness.infra_error is not None
    )
    task_tree = record.provenance.task_tree_sha256
    image_digest = record.provenance.image_digest
    if not experiment_id or not has_evidence or not task_tree or not image_digest:
        return None
    return experiment_id, record.task_id, record.trial, task_tree, image_digest


def _median_delta(
    pairs: list[tuple[RunRecord, RunRecord]],
    getter: Callable[[RunRecord], float | int | None],
) -> float | None:
    deltas = []
    for baseline, candidate in pairs:
        left = getter(baseline)
        right = getter(candidate)
        if left is not None and right is not None:
            deltas.append(float(right) - float(left))
    return statistics.median(deltas) if deltas else None


def _paired(
    grouped: dict[str, list[RunRecord]], baseline: str, candidate: str
) -> PairedDelta | None:
    by_key: dict[str, dict[tuple[str, str, int, str, str], RunRecord]] = {}
    duplicate_keys: dict[str, set[tuple[str, str, int, str, str]]] = {}
    for name in (baseline, candidate):
        records = grouped.get(name, [])
        candidates: dict[tuple[str, str, int, str, str], list[RunRecord]] = defaultdict(list)
        for record in records:
            if (key := _pair_key(record)) is not None:
                candidates[key].append(record)
        duplicate_keys[name] = {
            key for key, matches in candidates.items() if len(matches) > 1
        }
        indexed = {
            key: matches[0]
            for key, matches in candidates.items()
            if len(matches) == 1
        }
        by_key[name] = indexed
    keys = sorted(set(by_key[baseline]) & set(by_key[candidate]))
    if not keys:
        return None
    pairs = [(by_key[baseline][key], by_key[candidate][key]) for key in keys]
    wins = ties = losses = 0
    deltas = []
    for left, right in pairs:
        delta = int(right.correctness.resolved) - int(left.correctness.resolved)
        deltas.append(delta)
        if delta > 0:
            wins += 1
        elif delta < 0:
            losses += 1
        else:
            ties += 1
    return PairedDelta(
        baseline=baseline,
        candidate=candidate,
        pairs=len(pairs),
        candidate_wins=wins,
        ties=ties,
        candidate_losses=losses,
        duplicate_keys_excluded=len(
            duplicate_keys[baseline] | duplicate_keys[candidate]
        ),
        resolved_rate_delta=statistics.fmean(deltas),
        wall_time_median_delta_s=_median_delta(
            pairs, lambda r: r.efficiency.wall_time_s
        ),
        token_median_delta=_median_delta(pairs, _total_tokens),
        cost_median_delta_usd=_median_delta(
            pairs, lambda r: r.efficiency.cost_usd
        ),
    )


def compare_agents(records: list[RunRecord]) -> AgentComparison:
    """Summarize records and produce only defensibly paired comparisons."""

    groups: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        groups[_agent_key(record)].append(record)
    summaries = [_summarize(groups[name]) for name in sorted(groups)]
    paired = []
    names = sorted(groups)
    for index, baseline in enumerate(names):
        for candidate in names[index + 1 :]:
            comparison = _paired(groups, baseline, candidate)
            if comparison is not None:
                paired.append(comparison)
    return AgentComparison(summaries=summaries, paired=paired)
