# Agent Eval on k3s

[![Assurance](https://github.com/wuchris-ch/agent-eval-k3s/actions/workflows/ci.yml/badge.svg)](https://github.com/wuchris-ch/agent-eval-k3s/actions/workflows/ci.yml)
[![Python 3.12–3.14](https://img.shields.io/badge/python-3.12%E2%80%933.14-3776AB)](pyproject.toml)
[![Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-2f855a)](LICENSE)

An evidence-first evaluation platform for AI coding and pull-request review
agents. It runs blinded, versioned tasks, keeps grading outside the agent's
control, distinguishes bad answers from broken infrastructure, and exports
privacy-filtered traces through OpenTelemetry and Phoenix.

[![Agent evaluation and observability architecture](docs/local-review-platform.svg)](https://wuchris-ch.github.io/agent-eval-k3s/)

[Open the interactive architecture explorer](https://wuchris-ch.github.io/agent-eval-k3s/)
to zoom, pan, use full screen, and move between the supporting system diagrams.

This repository contains the evaluation system. The separately deployable
reviewer lives in [`pr-review-agent`](https://github.com/wuchris-ch/pr-review-agent).

## Latest validated benchmark

The release-quality benchmark runs every one of the 20 reviewer cases three
times. This result was recorded on September 3, 2026 with `gemini-3.8-flash`
against reviewer corpus `v1.1.0`. Raw model responses stay local.

| Metric | Result | Required gate |
|---|---:|---:|
| Release gate | **PASS** | `PASS` |
| Overall grade | **A** | A |
| Average score | **1.000** | ≥ 0.900 |
| Accepted evaluations | **60/60** | Informational |
| Infrastructure errors | **0** | 0 |
| Security-blocker recall | **100%** (21/21) | 100% |
| Clean-diff accuracy | **100%** (21/21) | ≥ 95% |
| Case stability | **100%** (20/20) | 100% |
| First-pass acceptance | **95%** (57/60) | Informational |

See the [complete versioned result](benchmarks/reviewer-corpus/v1/results/2026-09-03.md)
for run identities, report digests, and every case and trial.

## What makes the evaluation trustworthy

- **Blind execution.** The target receives the raw diff or task workspace, not
  the case ID, golden findings, scoring threshold, or expected answer.
- **Independent evidence.** The harness owns hidden tests, scanners, golden
  matches, acceptance policy, and the final result.
- **Explicit failure semantics.** `accepted`, `rejected`, and `infra_error`
  prevent a broken model request from being counted as a clean result.
- **Reproducible inputs.** Corpus artifacts, expected findings, task images,
  commands, and reports are bound to hashes and versioned metadata.
- **Honest correction metrics.** First attempts and critique-guided corrections
  are recorded separately, so retries cannot rewrite the baseline.
- **Privacy-aware observability.** Traces retain scores, latency, attempts, and
  counts while excluding prompts, diffs, completions, credentials, and private
  endpoints.

## Evaluation modes

| Mode | Target | Evidence |
|---|---|---|
| Reviewer benchmark | External review-agent executable | 20 golden diffs, exact finding matches, block decisions, stability, optional GEval |
| Coding-agent run | Agent working inside k3s | Hidden tests, coverage, Semgrep, Gitleaks, Trivy, Ruff, challenges, optional judge |
| Existing workspace | Already-produced code | The same evaluator without launching an agent |

### Reviewer benchmark

Each case binds a raw unified diff to expected file, category, severity, and
changed-line ranges. Deterministic scoring combines finding F1 with the expected
block decision. A valid rejected result may receive one critique-guided retry,
but both attempts remain in the report.

The strict cohort gate requires:

- zero infrastructure errors;
- 100% recall for security-blocker goldens;
- at least 95% accuracy on clean diffs; and
- identical verdict signatures across all three trial rounds.

An optional DeepEval GEval judge can make a score stricter, but it cannot
override failed deterministic evidence.

### Isolated coding-agent evaluation

The strongest task mode is `isolated-black-box`:

1. The agent receives a prompt and starter workspace in its own pod.
2. Hidden tests remain inside a separate evaluator image.
3. The produced application is exposed through one declared TCP port.
4. The evaluator runs hidden tests, coverage, scanners, and policy checks.
5. The agent cannot edit its evaluator or final result.

## Quick start

Requirements on macOS:

```sh
brew install uv kubectl k3d gitleaks trivy
uv sync --frozen
uv run agent-eval doctor
```

### Run the reviewer benchmark locally

Install the sibling reviewer once with `npm link`, configure an
OpenAI-compatible model gateway, then run:

```sh
./evaluate-reviewer
```

The command starts Phoenix and the OpenTelemetry Collector with Docker Compose,
runs one trial across all 20 cases, writes `review-agent-eval.json`, and prints
the dashboard URL. For a release-quality run:

```sh
EVAL_TRIALS=3 ./evaluate-reviewer
```

For a fast smoke test:

```sh
EVAL_CASE=auth-bypass ./evaluate-reviewer
```

### Run the complete local k3s platform

The k3s stack keeps the PR worker, Phoenix, and OpenTelemetry Collector running
and schedules the three-trial benchmark for 2:00 AM Vancouver time.

```sh
./review-stack sync-secrets
./review-stack up
```

Normal operations are intentionally short:

```sh
./review-stack status      # deployments, scheduler, jobs, and pods
./review-stack dashboard   # open Phoenix
./review-stack eval        # run the complete benchmark now
./review-stack results     # copy the newest report into this checkout
./review-stack logs        # recent PR worker activity
./review-stack pause       # pause reviews and scheduled evaluation
./review-stack resume      # resume both
```

The worker polls configured GitHub repositories once per minute and reviews
each new PR head SHA once. Credentials and repository names are synchronized
into a local Kubernetes Secret. A local `.review-repositories` file may hold a
private comma-separated watch list and is ignored by Git.

On a Mac, the cluster pauses when the machine or Docker Desktop sleeps. The k3d
containers use `unless-stopped`, and Kubernetes restores the declared workloads
when Docker returns.

## Outputs and observability

The detailed JSON report is the authoritative record. It contains every trial,
first and corrected attempt, score component, outcome, and timing measurement.
A content-minimized Markdown record can be published with:

```sh
./record-review-eval gemini-3.8-flash
```

OpenTelemetry spans flow through a checked-in Collector configuration before
reaching Phoenix. The projection includes corpus identity, case, trial,
outcome, attempt count, scores, latency, finding counts, and a command digest.
The Collector removes prompt, completion, provider, server-address, and
authorization attributes.

Local result files remain authoritative if telemetry is unavailable.

## Outcome model

| Outcome | Meaning |
|---|---|
| `accepted` | The target completed and met every configured requirement. |
| `rejected` | The target completed, but its evidence failed a quality gate. |
| `infra_error` | The harness could not collect trustworthy evidence. |

Infrastructure errors contribute zero to cohort averages, so broken execution
cannot inflate a grade. Grades are A at `0.90`, B at `0.75`, C at `0.60`, and F
below `0.60`.

## Repository map

```text
benchmarks/        versioned reviewer corpora, goldens, and public records
deploy/            evaluator image and local k3s manifests
observability/     Docker Compose and privacy-filtering OTel configuration
src/agent_eval/    runner, evaluators, policy, evidence, and reporting
tasks/             isolated coding-agent tasks and hidden evaluators
tests/             unit, integration, adversarial, and assurance tests
```

## Development

```sh
uv run ruff check .
uv run pytest
uv build --no-sources
```

The CI matrix tests Python 3.12, 3.13, and 3.14, builds the isolated task
images, verifies embedded agent binaries, runs pinned secret and vulnerability
scanners, and proves the packaged wheel works without the source tree.

See [DETAILS.md](DETAILS.md) for the exact contracts, metrics, governance,
attestation, isolation model, and security boundaries.

## License

Apache-2.0. See [LICENSE](LICENSE).
