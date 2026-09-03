# Agent Eval reference

This project is an evaluation harness. It does not ship an agent that reviews
pull requests or writes production code.

## Evaluation modes

### External review-agent evaluation

`agent-eval eval-review-agent` evaluates a separately installed executable.
The harness owns the golden corpus, input binding, deterministic score, outcome,
retry policy, cohort grade, result file, and telemetry.

The child receives only the raw unified diff and an explicit environment
allowlist. It does not receive the corpus case ID, expected findings, scoring
threshold, or task metadata. On an optional second attempt,
`AGENT_EVAL_FEEDBACK` contains the first attempt's critique. The diff remains
unchanged.

The output contract is versioned and strict. `input_sha256` binds the response
to the exact input. A contract failure or nonzero process exit is
`infra_error`.

Deterministic matching uses exact normalized file and category plus a line
inside the golden range. It records TP, FP, FN, precision, recall, F1, and
whether the block decision matches blocker or major goldens. The case score is
the mean of finding F1 and the block-decision score. When GEval is enabled, the
final score is the lower of the deterministic and model-judge scores.

First and corrected attempts are stored separately. Cohort averages include
infrastructure errors as zero and map to A, B, C, or F.

### Isolated coding-agent evaluation

`agent-eval run` creates an isolated k3s execution environment, runs a coding
agent against a versioned task, and evaluates the resulting workspace outside
the agent pod.

The strongest task mode is `isolated-black-box`:

1. The agent sees the prompt and starter workspace.
2. Hidden tests stay in the evaluator image.
3. The submission and evaluator run in different pods.
4. The evaluator reaches the submission only through the declared TCP port.
5. Tests, scanners, challenges, and policy produce the final outcome.

`agent-eval evaluate` runs the evaluator against an existing workspace without
launching an agent. This is useful for negative controls and harness checks.

## Outcomes

The outcome model is shared across evaluation modes:

| Status | Meaning |
|---|---|
| `accepted` | Required evidence was collected and all configured gates passed. |
| `rejected` | Evaluation completed and the target failed one or more gates. |
| `infra_error` | Required evidence could not be collected or authenticated. |

A missing measurement stays missing. It is not converted to zero, success, or
an estimate unless the cohort rule explicitly assigns zero to an infrastructure
error.

## Versioned datasets

Coding tasks live under `tasks/<task-id>`. A task binds:

- prompt and starter workspace;
- container build context;
- hidden-test command and evaluator mode;
- scanner and judge settings;
- acceptance thresholds;
- dataset identity and revision;
- time, resource, and network limits.

Review-agent goldens live under `benchmarks/reviewer-corpus/<version>`. The
corpus binds:

- raw diff artifacts;
- expected findings and changed-line ranges;
- artifact hashes;
- clean or faulty labels;
- optional executable reproducers.

Static corpus validation verifies hashes, paths, case alignment, and golden
locations. Reproducers run only with `--allow-local-execution`.

## Evidence and metrics

Each coding-agent run stores a complete `results.json` and a queryable row in
`metrics.db`. Available evidence can include:

- wall time, exit status, timeouts, and diff size;
- token, turn, tool, and cost fields when the target exposes them;
- hidden-test counts and coverage;
- scanner identity, status, and findings;
- challenge checks and judge dimensions;
- task, image, model, evaluator, and dataset identity;
- governance and attestation records.

Run:

```sh
uv run agent-eval verify-run --run <run-id>
```

to verify a saved run against its bound artifacts.

Review-agent evaluation writes a separate JSON cohort result containing every
first and corrected attempt. The installed target's command is represented by
an argv SHA-256 digest in telemetry.

## OpenTelemetry

Telemetry is optional and best-effort. Local result files remain authoritative.
`./evaluate-reviewer` starts the local stack and enables export automatically.
For manual OTLP export to that stack, use:

```sh
export AGENT_EVAL_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318"
```

The external-agent projection excludes raw diff content, output JSON, goldens,
feedback, details, rationales, and judge reasons. It exports bounded identifiers
and numeric operational metrics. The local Collector performs a second privacy
pass that removes content, provider identity, server addresses, and
authorization attributes before storing traces in Phoenix.

## Optional model judge

External review-agent GEval uses a generic OpenAI-compatible gateway:

```sh
uv sync --extra judge
export MODEL_GATEWAY_API_KEY="..."
export MODEL_GATEWAY_BASE_URL="https://gateway.example/v1"
export AGENT_EVAL_JUDGE_MODEL="your-model-id"
```

No provider-specific endpoint is embedded in the repository. The deterministic
evaluation path does not need these settings. `--judge` sends the diff, agent
output, and expected result to the configured gateway, so it is an explicit
data-egress choice.

## Security boundaries

- External agent commands use argv with `shell=False`.
- The child environment is an allowlist, not the full host environment.
- Diffs, stdout, stderr, and runtime are bounded.
- External review-agent v1 inputs are capped at 64 KiB before process launch.
- A timeout kills the whole child process group.
- Strict JSON rejects duplicate keys, trailing text, unknown fields, invalid
  paths, non-finite values, and inconsistent risk decisions.
- Corpus commands are data until the user explicitly allows execution.
- Hidden tests and evaluator outputs stay outside coding-agent workspaces.
- Telemetry failure never changes a completed evaluation outcome.

This project is a local evaluation tool, not a hardened multi-tenant sandbox.
Use trusted infrastructure and review task or corpus commands before enabling
execution.

## Main commands

```text
agent-eval eval-review-agent   evaluate an external review-agent command
agent-eval run                 run and evaluate a coding agent in k3s
agent-eval evaluate            evaluate an existing workspace
agent-eval corpus validate     validate a golden review corpus
agent-eval tasks validate      validate a coding task and oracle
agent-eval compare             compare completed cohorts
agent-eval report              report saved task runs
agent-eval verify-run          verify persisted evidence
agent-eval doctor              inspect local prerequisites
```

The `benchmark-review` and `benchmark-experiment` commands remain offline
evaluation utilities for previously generated external-agent outputs. They do
not run or implement a review agent.
