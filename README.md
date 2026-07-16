# agent-eval-k3s

Evaluate pull requests and coding agents using tests and other recorded
evidence.

![A simple overview of pull-request review and coding-agent evaluation](docs/agent-eval-overview.svg)

## What it does

| Command | What it does | Main advantage |
|---|---|---|
| `agent-eval review` | Reviews a code change without Kubernetes | Tests and scanners back up model findings |
| `agent-eval run` | Grades Claude Code or Codex in Kubernetes | Hidden tests grade the result independently |

Both commands collect evidence before making a decision. If required evidence
is missing, the command fails instead of pretending the result is clean.
This makes the result easier to explain and reproduce than a model opinion or
a single benchmark score.

## Measured end-to-end check

On July 15, 2026, Codex with requested model `gpt-5.6-sol` was run three
times per case on an Apple M1 Max:

| `review` case | Result | Blocked | Median command time |
|---|---:|---:|---:|
| Clean refactor | 3/3 low risk | 0/3 | 25.05 s |
| Authorization bypass | 3/3 high risk | 3/3 | 24.79 s |

| `run` task | Accepted | Hidden tests | Challenge checks | Median agent time | Median total tokens |
|---|---:|---:|---:|---:|---:|
| Todo API | 3/3 | 33/33 | n/a | 41.1 s | 67,923 |
| Agentic safety controls | 3/3 | 15/15 | 30/30 | 39.2 s | 76,885 |

All six agent runs were scanner-clean. These are small smoke checks, not model
rankings. The second task tests enterprise-style controls on a small codebase,
not enterprise scale. See the [method, per-trial data, and
limits](benchmarks/measured/2026-07-15/README.md).

## Quick start

Install the project and its command-line tools on macOS:

```sh
git clone https://github.com/wuchris-ch/agent-eval-k3s.git
cd agent-eval-k3s

brew install uv kubectl k3d gitleaks trivy
uv sync
uv run agent-eval doctor
uv run agent-eval scanners prepare
```

You also need Docker. To run an agent, provide one of these:

- `ANTHROPIC_API_KEY` for Claude Code.
- A Codex login created with `codex login`.

### Review a code change

No cluster is needed:

```sh
uv run agent-eval review \
  --repo /path/to/repository \
  --base main \
  --head my-branch
```

The command writes `review.md`, `review.json`, and `review.sarif` under
`<repo>/.agent-eval/reviews/`. The report explains why the change is low,
medium, or high risk.

Tests and checks run directly on your Mac only when you pass
`--allow-local-execution`. Use that flag only for code you trust.

The review reads the base and head Git objects, loads policy from the trusted
base, and collects test, scanner, and model-review evidence. Model findings are
checked against the changed lines before they can affect the risk level.

Unlike a model-only review, a confident but unsupported AI claim cannot raise
the risk level. Deterministic blockers do not depend on model agreement.

### Test a coding agent

`run` starts a local k3d cluster when needed:

```sh
uv run agent-eval run \
  --task example-todo-api \
  --agent codex \
  --trials 3 \
  --experiment-id todo-july-2026 \
  --gate
```

Use `--agent claude-code` to test Claude Code instead.

Compare completed runs:

```sh
uv run agent-eval compare --task example-todo-api --out comparison.json
uv run agent-eval report --task example-todo-api
```

Unlike a one-number benchmark, repeated trials keep the evidence behind the
score and separate agent failures from harness failures.
This also complements public leaderboards by testing your own versioned tasks
and acceptance rules.

## How an agent run works

1. The harness loads a versioned task with a prompt and starter code.
2. Claude Code or Codex edits the code inside an agent pod. It cannot see the
   hidden tests.
3. When the agent stops, the harness stops leftover processes and copies out
   the workspace.
4. A separate evaluator tests the produced program. The harness also checks
   the diff and runs the configured scanners.
5. The acceptance policy combines the evidence and records `accepted`,
   `rejected`, or `infra_error`.

The submission and evaluator are separate in protected runs. The evaluator
owns the hidden tests and results. It can contact the submission only through
the task's declared TCP port.

This is safer than grading inside the agent's workspace: the agent cannot edit
the hidden tests or write its own passing result.

## Where the metrics come from

There is no separate metrics service. The harness combines a few direct
sources:

| Source | What it provides |
|---|---|
| Harness | Wall time, exit code, timeout, and diff size |
| Claude Code or Codex JSON events | Tokens, turns, tool calls, and model identity when the CLI exposes it |
| Evaluator | Test results and coverage |
| Scanners and judge | Code-quality, security, secret, vulnerability, and rubric results |

Claude Code reports cost in its final event. Codex subscription use does not
provide a trustworthy per-run cost, so Codex cost is stored as `null`.

The harness does not invent missing values. For example, if one Codex turn has
no valid token count, the total token fields stay `null` instead of reporting a
partial total.

That is more honest than filling gaps with estimates. It also keeps model
quality separate from missing provider telemetry.

Each run is saved in two main forms:

- `results.json` is the complete run record.
- `metrics.db` is the queryable view used by `compare` and `report`.

Use this command to check a saved run against its artifacts:

```sh
uv run agent-eval verify-run --run <run-id>
```

## What the outcomes mean

| Outcome | Meaning |
|---|---|
| `accepted` | The run completed and every configured requirement passed. |
| `rejected` | The run completed, but a test, quality, safety, or budget rule failed. |
| `infra_error` | The harness could not collect enough trustworthy evidence to judge the agent. |

Passing the hidden tests is not always enough. A run can still be rejected for
a detected secret, a scanner failure, or an exceeded budget.

Keeping `rejected` separate from `infra_error` also prevents a broken cluster
from being counted as a bad agent result.

## Need the full reference?

Read [DETAILS.md](DETAILS.md) for:

- pull-request graders and reviewer benchmarks;
- pod, network, scanner, and credential controls;
- governed runs, image identity, audit, and provenance;
- task and agent-adapter authoring;
- state paths, OpenTelemetry, configuration, and project layout.

The detailed reference also states the security boundaries and the guarantees
the project does not make.
