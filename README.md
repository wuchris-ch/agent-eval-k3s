# agent-eval-k3s

An evaluation-only harness for AI agents. It runs repeatable golden cases,
records evidence, assigns `accepted`, `rejected`, or `infra_error`, and can
export content-minimized OpenTelemetry spans.

![Local k3s review and evaluation platform](docs/local-review-platform.svg)

This repository does not contain a pull-request review agent. It can evaluate
one that is installed separately, such as
[`pr-review-agent`](https://github.com/wuchris-ch/pr-review-agent).

## What it evaluates

| Target | Command | Main evidence |
|---|---|---|
| External review agent | `agent-eval eval-review-agent` | Golden diffs, exact finding matches, block decisions, optional GEval |
| Coding agent | `agent-eval run` | Hidden tests, scanners, challenge checks, optional judge |
| Existing workspace | `agent-eval evaluate` | The same evaluator without running an agent |

The harness keeps evaluator failures separate from bad agent results. A
nonzero agent exit, invalid JSON, missing evidence, or broken environment is
`infra_error`, never a clean result.

## Quick start

On macOS:

```sh
git clone https://github.com/wuchris-ch/agent-eval-k3s.git
cd agent-eval-k3s

brew install uv kubectl k3d gitleaks trivy
uv sync --frozen
uv run agent-eval doctor
```

Docker is used for the local Phoenix and OpenTelemetry stack and for the
isolated k3s coding-agent flow.

## Evaluate an external review agent

After installing the sibling reviewer with `npm link`, the normal command is:

```sh
./evaluate-reviewer
```

This one command uses Docker Compose for the smallest local workflow:

1. starts or verifies the local Phoenix and OpenTelemetry services;
2. enables privacy-filtered OTLP export;
3. runs one trial across the 20-case reviewer corpus;
4. writes `review-agent-eval.json`; and
5. prints the local Phoenix dashboard URL.

The observability services use `restart: unless-stopped`, so Docker keeps them
running between evaluations. Use `EVAL_TRIALS=3 ./evaluate-reviewer` for a
slower release-quality stability run. Set `EVAL_OBSERVABILITY=0` for a
result-only run without Docker.

For continuous PR reviews, a nightly three-trial evaluation, persistent
results, and the same Phoenix dashboard in local k3s, use `review-stack`
instead. See [Local k3s operation](#local-k3s-operation).

For a fast single-case smoke check:

```sh
EVAL_CASE=auth-bypass ./evaluate-reviewer
```

The full command below documents what the shortcut runs internally.

The target is an executable owned and installed outside this repository:

```sh
uv run --extra observability agent-eval eval-review-agent \
  --command "pr-review-agent" \
  --corpus benchmarks/reviewer-corpus/v1/corpus.yaml \
  --trials 1 \
  --out review-agent-eval.json
```

This command runs locally and must be trusted. The harness uses `shell=False`
and terminates its dedicated process group after each attempt, but it is not a
host sandbox.

For every case, the harness writes the exact raw unified diff to the target's
standard input. It does not pass the case name, expected answer, threshold, or
golden findings to the child process.

Version 1 caps each raw diff at 64 KiB. Larger cases fail closed before the
target starts. A later contract can add negotiated per-agent capabilities.

The target must write one JSON object to standard output:

```json
{
  "schema_version": "1.0",
  "input_sha256": "<lowercase SHA-256 of the exact raw diff>",
  "risk": "high",
  "blocked": true,
  "findings": [
    {
      "severity": "blocker",
      "category": "security",
      "file": "src/auth.py",
      "line": 42,
      "detail": "Authorization can be bypassed."
    }
  ],
  "rationale": "The change removes an authorization check."
}
```

The contract is strict:

- A blocker means `risk: high` and `blocked: true`.
- A major finding without a blocker means `risk: medium` and `blocked: true`.
- Minor or info findings only mean `risk: low` and `blocked: false`.
- Finding severity is `blocker`, `major`, `minor`, or `info`.
- Finding category is `security`, `correctness`, `style`, or `performance`.
- Files are repository-relative and lines are positive integers.
- Unknown fields, duplicate keys, trailing text, non-finite numbers, and an
  incorrect `input_sha256` are rejected.
- A nonzero exit or invalid output is `infra_error`.

Standard input is the default and preferred cross-repository contract. Use
`--diff-file-flag=--diff` when the target accepts a temporary diff path.

The child environment is allowlisted. Generic target configuration includes
`MODEL_GATEWAY_API_KEY`, `MODEL_GATEWAY_BASE_URL`, and `REVIEW_AGENT_MODEL`.
Custom certificate stores can use `SSL_CERT_FILE`, `SSL_CERT_DIR`, or
`NODE_EXTRA_CA_CERTS`. Safe OTLP endpoint, protocol, exporter, and service-name
settings are forwarded so the target can emit traces. OTLP headers and resource
attributes are not forwarded because they can contain credentials or private
metadata.

### Scoring and correction

Deterministic scoring compares file, category, severity, and line against the
versioned goldens. It combines finding F1 with the expected block decision.
The default acceptance threshold is `0.6`.

After a valid rejected first attempt, the harness can run one corrected
attempt. The raw diff remains byte-for-byte unchanged. Prior critique is sent
only as `AGENT_EVAL_FEEDBACK`. Results keep first-attempt and corrected-attempt
metrics separately, so the correction never rewrites the baseline.

Disable correction with `--no-self-correct`.

### Optional DeepEval GEval judge

The deterministic score works offline. To add a model judge:

```sh
export MODEL_GATEWAY_API_KEY="..."
export MODEL_GATEWAY_BASE_URL="https://gateway.example/v1"  # optional
export AGENT_EVAL_JUDGE_MODEL="your-model-id"

EVAL_JUDGE=1 ./evaluate-reviewer
```

GEval can tighten a result but cannot override failed golden evidence. The
gateway settings are generic and work with an OpenAI-compatible endpoint.
Enabling `--judge` sends the diff, output, and golden expectation to that
endpoint. Leave it disabled when those materials must stay local.

## Evaluate coding agents in k3s

The existing isolated task flow remains evaluation infrastructure:

```sh
uv run agent-eval run \
  --task example-todo-api \
  --agent codex \
  --trials 3 \
  --experiment-id todo-example \
  --gate
```

Each run uses a versioned task and starter workspace. The agent edits code in
an isolated pod, then a separate evaluator runs hidden tests and configured
scanners. The agent cannot edit the hidden tests or its final result.

Evaluate an already-produced workspace without launching an agent:

```sh
uv run agent-eval evaluate \
  --task example-todo-api \
  --workspace /path/to/workspace \
  --gate
```

Compare and verify saved results:

```sh
uv run agent-eval compare --task example-todo-api --out comparison.json
uv run agent-eval report --task example-todo-api
uv run agent-eval verify-run --run <run-id>
```

## Local k3s operation

The evaluator is intentionally a batch job. It should run against a fixed
corpus, record a result, and exit. Keeping an evaluation process alive would
waste model calls and make results harder to compare.

The included local stack keeps the PR worker, Phoenix, and the OpenTelemetry
Collector running in k3s. It also schedules the complete 20-case corpus for
three trials every day at 2:00 AM Vancouver time. On a Mac, the services pause while the
machine or Docker Desktop sleeps and recover when the k3d cluster returns.

After the sibling reviewer is checked out next to this repository and runtime
credentials have been synchronized, the normal commands are:

```sh
./review-stack up          # build and start everything
./review-stack status      # show worker, scheduler, jobs, and pods
./review-stack dashboard   # open Phoenix
./review-stack eval        # run the complete three-trial evaluation now
./review-stack results     # copy the latest k3s report into this checkout
./review-stack logs        # show recent PR worker activity
./review-stack pause       # pause reviews and nightly evaluation
./review-stack resume      # resume both
```

The worker polls the configured GitHub repositories once per minute. It reviews
each new PR head SHA once, publishes a review comment, and sets a commit status.
The head-specific marker prevents duplicate reviews after pod restarts.

Runtime credentials are stored only in a local Kubernetes Secret. They are not
written to a manifest or container image. The local `review-eval` shell helper
can synchronize them without printing them:

```sh
EVAL_K3S_SYNC=1 review-eval
```

The repository watch list follows the same local-only path. Set
`GITHUB_REPOSITORIES` to a comma-separated list before synchronizing, or put
that list on one line in `.review-repositories`. The file is ignored by Git so
private repository names do not become part of the public project.

The simpler Compose stack remains available for one-off evaluation:

```sh
docker compose -f observability/compose.yaml up -d --wait
```

## Outcomes

| Outcome | Meaning |
|---|---|
| `accepted` | The target completed and met every configured requirement. |
| `rejected` | The target completed, but its evidence did not meet the threshold or policy. |
| `infra_error` | The harness could not collect trustworthy evidence. |

Infrastructure errors count as zero in cohort averages, so broken executions
cannot inflate a grade. Cohort grades are A at `0.90`, B at `0.75`, C at
`0.60`, and F below `0.60`.

### Versioned baseline records

The detailed JSON report is local because it contains model-written review
text. A content-minimized Markdown record can be committed safely after a
release-quality run:

```sh
./record-review-eval gemini-3.8-flash
```

The record includes run identity, aggregate gates, first-pass quality,
per-case trial results, and a digest of the local raw report. It excludes raw
diffs, model responses, credentials, endpoints, and provider identity.

## OpenTelemetry

`./evaluate-reviewer` starts and configures the checked-in local stack
automatically. For another command that exports to the same stack, use:

```sh
export AGENT_EVAL_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:4318"
```

External-agent spans include corpus identity, case ID, trial number, outcome,
attempt count, scores, latency, finding counts, and a hash of the command argv.
They do not export raw diffs, agent output, goldens, feedback, or judge reasons.
The Collector also removes prompt, completion, provider, server-address, and
authorization attributes before local storage. The JSON evaluation result
remains the detailed local record.

## Corpus and task checks

Validate corpus hashes and golden locations without executing its reproducer:

```sh
uv run agent-eval corpus validate \
  benchmarks/reviewer-corpus/v1/corpus.yaml
```

Run a reproducer only after reviewing and trusting it:

```sh
uv run agent-eval corpus validate \
  benchmarks/reviewer-corpus/v1/corpus.yaml \
  --allow-local-execution
```

See [DETAILS.md](DETAILS.md) for task isolation, metrics, governance,
attestations, state paths, and security boundaries.
