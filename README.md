# agent-eval-k3s

Change-assurance and coding-agent evaluation harness. Two modes:

- **`agent-eval review`** — a pre-merge change report for any git repo (AI- or
  human-authored), built on executable graders in the style of frontier code
  evals: scope/command/test graders, scanners over the changed files, and an
  LLM review whose findings must survive evidence verification. No cluster,
  no Docker, no task setup.
- **`agent-eval run`** — a k3s benchmark harness that launches coding agents in
  isolated pods and scores their output with hidden tests, scanners, and an
  LLM judge; agent efficiency (tokens, turns, wall time, diff size) becomes
  metadata on every change.

The project is designed to make coding-agent results reproducible: each task
defines the starter workspace, hidden tests, runtime image, oracle solution, and
rubric. Each trial launches a fresh sandbox, captures the agent transcript,
evaluates the produced workspace in a second clean pod, and persists metrics for
comparison across runs.

Inspired by the good parts of Terminal-Bench/Harbor (task-as-directory format,
sandbox-per-run), SWE-bench (pass/fail grounded in executable tests, pass@k),
and OpenHands (pluggable agent adapters, transcript-derived cost metrics).

## Highlights

- Runs agent attempts in disposable k3d/k3s pods, separate from the host
  checkout.
- Re-evaluates produced code in a fresh pod so the agent cannot tamper with the
  hidden test environment.
- Supports both full agent runs and eval-only scoring for workspaces produced
  elsewhere.
- Tracks correctness, pass@k, coverage, wall time, token usage, tool calls,
  diff size, scanner findings, and judge scores in SQLite-backed run records.
- Provides pluggable adapters for Claude Code and OpenAI Codex CLI.

## Resume summary

Built a Kubernetes-backed coding-agent evaluation harness that runs autonomous
agents in isolated k3s pods, snapshots their code changes, validates them with
hidden tests in clean evaluation pods, and records correctness, efficiency,
security, and LLM-judge metrics across repeated trials.

## How it works

Each trial runs a pipeline:

1. **Agent phase** — a pod is created from the task's environment image with the
   starter workspace but no tests. The agent runs headless (`claude -p ...
   --output-format stream-json`) with its API key injected from a k8s secret.
   The transcript is captured for token/cost/turn metrics.
2. **Snapshot** — the workspace is pulled out of the pod and diffed against the
   starter state.
3. **Eval phase** — a *fresh* pod gets the produced workspace plus the hidden
   tests; the task's test command runs and junit/coverage results are parsed.
   The fresh pod guarantees the agent could not have poisoned the test env.
4. **Scan phase** — host-side ruff, semgrep, gitleaks, and trivy over the
   produced workspace (each degrades gracefully if not installed).
5. **Judge phase** — the Claude API scores the diff against the task prompt on
   the task's rubric (spec adherence, maintainability, test quality).
6. **Persist** — everything lands in `runs/<run-id>/` plus a SQLite row.

## Prerequisites

- Docker (colima works), kubectl, [k3d](https://k3d.io) (`brew install k3d`)
- `uv` for Python
- Credentials for at least one agent/judge:
  - `ANTHROPIC_API_KEY` exported (claude-code agent + claude judge), and/or
  - a logged-in `codex` CLI (`codex login`, ChatGPT subscription works) for the
    codex agent + codex judge
- Optional scanners: `brew install gitleaks trivy` (semgrep/ruff run via `uvx`)

## Quick start

```sh
uv sync
uv run agent-eval doctor        # shows what's installed and what it unlocks
```

Review a change in any git repo (no cluster needed):

```sh
uv run agent-eval review                              # working tree vs main
uv run agent-eval review --base main --head my-branch
uv run agent-eval review --test-cmd "pytest -q" --check "ruff check ." \
    --context @ticket.md
uv run agent-eval review --test-cmd "pytest -q" --gen-tests   # + generated test
```

The report (terminal + `review.md`/`review.json` under
`<repo>/.agent-eval/reviews/`) gives an overall low/medium/high risk, changed
files by subsystem, deterministic risk signals, scanner findings, grader
results, and a verified-findings LLM review. Exit code is 2 when risk is high
or any blocking grader fails, so it drops into CI as a check.

### Review graders

The review is built on executable graders modeled on frontier code evals
(Cognition's FrontierCode), not on a single LLM opinion pass:

| Grader | Checks | Passes when |
|---|---|---|
| scope | policy file boundaries and diff size | diff within constraints |
| command (`--check`) | build/lint/typecheck commands | exit code 0 |
| classical (`--test-cmd`) | the test suite on the head side | tests pass |
| reverse-classical | new/changed tests replayed against the base commit | they FAIL there (tests that also pass on base don't verify the new behavior) |
| generated test (`--gen-tests`) | an LLM-written discriminating test, with one adaptive repair pass | passes on head AND fails on base |
| prompt (LLM review) | findings, each with a verbatim diff quote | quote verified programmatically, then blocker/major findings re-confirmed by an adversarial second pass |

Blocking graders (command, head tests, blocked/allowed paths, secrets) gate
the change: a failure forces risk to high and exit code 2. Non-blocking
failures (size limits, weak tests) add weighted risk signals. Unverifiable or
rejected LLM findings are kept in `review.json` but never affect risk, so the
review cannot hallucinate its way to a verdict. `--gen-tests` runs
LLM-generated code on your machine: use it only on changes you trust, or wait
for the sandboxed (k3s) execution mode.

With `--head <ref>`, tests and checks run in a clean temporary worktree of
that ref, so the test command must work in a fresh checkout (`uv run ...`,
`uvx pytest`, `npx ...` style commands do).

Per-repo policy lives in `<repo>/.agent-eval.yaml`:

```yaml
review:
  test_cmd: "uv run pytest -q"
  checks:
    - "uv run ruff check ."
  blocked_paths:        # blocking: changes here fail the review
    - ".github/workflows/*"
  allowed_paths: []     # if set, all changes must match one (blocking)
  max_files: 30         # non-blocking size limits
  max_lines: 800
  require_tests_for:    # code changes here without test changes get flagged
    - "src/*"
```

Patterns are fnmatch globs against the repo-relative path (`*` crosses `/`).

Benchmark an agent in k3s (`run` creates the cluster on first use):

```sh
uv run agent-eval run --task example-todo-api --agent codex --trials 1
uv run agent-eval run --task example-todo-api --agent claude-code --trials 1  # needs ANTHROPIC_API_KEY
uv run agent-eval report
```

Agent adapters: `claude-code` (auth via the `ANTHROPIC_API_KEY` k8s secret) and
`codex` (auth via your host `~/.codex/auth.json`, copied into the pod per run,
so a ChatGPT subscription login is enough; no API key needed).

> Credential exposure note: whichever auth reaches the agent pod (API key env
> var or codex auth.json) is readable by the agent under evaluation, which runs
> arbitrary code. Fine for a local harness evaluating trusted agents; treat
> untrusted agents accordingly (restrict egress, use throwaway credentials).

Eval-only mode (score code produced elsewhere):

```sh
uv run agent-eval evaluate --task example-todo-api --workspace /path/to/produced
```

## Metrics tracked

| Category    | Metrics |
|-------------|---------|
| Correctness | hidden tests passed/total, resolved, pass@k across trials, coverage |
| Efficiency  | wall time, input/output tokens, cost USD, turns, tool calls |
| Quality     | lint errors, semgrep findings by severity, secrets, dep vulns, diff size |
| Judge       | 1-5 per rubric dimension + weighted score + rationale |

## Writing a task

```
tasks/<task-id>/
├── task.yaml               # id, prompt, timeouts, test_command, judge weights
├── environment/
│   ├── Dockerfile          # toolchain + agent CLIs + COPY workspace /workspace
│   └── workspace/          # starter code the agent sees
├── tests/                  # hidden tests; mounted only in the eval pod at /tests
└── solution/               # oracle overlay; `tasks validate` requires it to pass
```

Conventions:
- `test_command` runs with cwd `/workspace`; hidden tests are at `/tests`; write
  junit XML to `/results/junit.xml` and (optionally) pytest-cov JSON to
  `/results/coverage.json`.
- The environment image must include the agent CLIs you want to evaluate
  (the claude-code adapter expects `claude` on PATH) and `tar`.
- Hidden tests should exercise only the public interface the prompt promises,
  and be order-independent (never assume a clean store).
- `agent-eval tasks validate <id>` proves the task by running the oracle
  solution through the real eval pipeline. Break the oracle on purpose once to
  confirm the task can also fail.

## Adding an agent adapter

Implement `AgentAdapter` (see `src/agent_eval/agents/base.py`): a shell command
that runs the agent against the prompt file with a machine-readable transcript
on stdout, plus a transcript parser producing `AgentMetrics`. Register it in
`src/agent_eval/agents/__init__.py`.

## Configuration

- `AGENT_EVAL_JUDGE` — judge backend: `claude`, `codex`, or `auto` (default:
  claude if `ANTHROPIC_API_KEY` is set, else codex if the CLI is logged in)
- `AGENT_EVAL_JUDGE_MODEL` — judge model override (claude backend defaults to
  `claude-sonnet-5`; codex backend uses your codex default unless this is set
  to a non-claude model)
- `--model` on `run` — model override passed to the coding agent
- Re-run `agent-eval cluster up` after changing `ANTHROPIC_API_KEY` to re-sync
  the k8s secret.
