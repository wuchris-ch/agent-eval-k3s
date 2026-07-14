from agent_eval.agent_comparison import compare_agents
from agent_eval.evaluators.tests import TestResults as EvalTestResults
from agent_eval.metrics import AgentMetrics, RunProvenance, RunRecord


def _run(agent, model, trial, resolved, *, experiment="exp-1", time=10, tokens=100):
    return RunRecord(
        run_id=f"{agent}-{trial}",
        task_id="task",
        agent=agent,
        trial=trial,
        experiment_id=experiment,
        correctness=EvalTestResults(
            total=1,
            passed=int(resolved),
            failed=int(not resolved),
            command_exit_code=0 if resolved else 1,
        ),
        efficiency=AgentMetrics(
            model=model,
            wall_time_s=time,
            tokens_in=tokens - 10,
            tokens_out=10,
            tool_calls=2,
        ),
        provenance=RunProvenance(
            task_tree_sha256="a" * 64,
            image_digest="sha256:" + "b" * 64,
        ),
    )


def test_comparison_has_denominators_intervals_and_paired_deltas():
    records = [
        _run("alpha", "a1", 1, False, time=12, tokens=100),
        _run("alpha", "a1", 2, True, time=10, tokens=120),
        _run("beta", "b1", 1, True, time=8, tokens=90),
        _run("beta", "b1", 2, True, time=9, tokens=100),
    ]

    result = compare_agents(records)

    alpha = result.summaries[0]
    assert alpha.sample_size == 2
    assert alpha.correctness_evidence_count == 2
    assert alpha.legacy_incomplete_count == 0
    assert alpha.resolved_rate == 0.5
    assert 0 <= alpha.resolved_wilson_95.lower < alpha.resolved_wilson_95.upper <= 1
    assert alpha.total_tokens.observed == 2
    assert alpha.total_tokens.median == 110
    assert alpha.cost_usd.completeness == 0
    assert alpha.sample_label == "small sample"

    paired = result.paired[0]
    assert paired.pairs == 2
    assert paired.candidate_wins == 1
    assert paired.ties == 1
    assert paired.candidate_losses == 0
    assert paired.resolved_rate_delta == 0.5
    assert paired.wall_time_median_delta_s == -2.5


def test_unpaired_historical_runs_are_not_presented_as_paired():
    records = [
        _run("alpha", "a1", 1, True, experiment=None),
        _run("beta", "b1", 1, False, experiment=None),
    ]

    result = compare_agents(records)

    assert len(result.summaries) == 2
    assert result.paired == []


def test_legacy_records_without_command_exit_are_not_misreported_as_failures():
    legacy = _run("alpha", "a1", 1, True)
    legacy.correctness.command_exit_code = None

    summary = compare_agents([legacy]).summaries[0]

    assert summary.correctness_evidence_count == 0
    assert summary.legacy_incomplete_count == 1
    assert summary.resolved_rate is None
    assert summary.resolved_wilson_95 is None


def test_pairing_requires_identical_task_and_image_identity():
    alpha = _run("alpha", "a1", 1, True)
    beta = _run("beta", "b1", 1, True)
    beta.provenance.task_tree_sha256 = "c" * 64

    assert compare_agents([alpha, beta]).paired == []


def test_duplicate_retries_are_excluded_from_pairing():
    records = [
        _run("alpha", "a1", 1, False),
        _run("alpha", "a1", 1, True),
        _run("beta", "b1", 1, True),
        _run("alpha", "a1", 2, True),
        _run("beta", "b1", 2, True),
    ]
    records[1].run_id = "alpha-retry"

    paired = compare_agents(records).paired[0]

    assert paired.pairs == 1
    assert paired.duplicate_keys_excluded == 1


def test_requested_model_is_not_collapsed_into_unknown():
    run = _run("alpha", None, 1, True)
    run.efficiency.requested_model = "requested-model"

    summary = compare_agents([run]).summaries[0]

    assert summary.model == "requested-model (requested, unobserved)"
