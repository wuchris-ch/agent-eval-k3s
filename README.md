# agent-eval-k3s

Evaluate code changes and coding agents with evidence, not just model opinions.

## Overview

`agent-eval-k3s` is a local-first assurance harness for software engineering
teams. It answers two practical questions:

1. **Should this pull request merge?** `agent-eval review` checks the diff with
   policy rules, commands, tests, scanners, and evidence-verified AI findings.
2. **How well did this coding agent perform?** `agent-eval run` gives the agent
   a repeatable task in Kubernetes, grades the result in a fresh pod, and saves
   a comparable scorecard.

Both paths use the same principle: important claims need machine-checkable
evidence. Missing evidence stays missing. It is never silently converted to a
clean result or a zero.

![Overview of the pull-request review and coding-agent evaluation workflows](docs/agent-eval-overview.svg)

### What you get

- Explicit `accepted`, `rejected`, or `infra_error` outcomes for agent trials.
- Low, medium, or high risk reports for pull requests, plus JSON and SARIF.
- Hidden-test correctness, scanner findings, coverage, latency, token use,
  cost, tool use, diff size, and judge results when those values are observable.
- Repeatable reviewer benchmarks with precision, recall, F1, false positives,
  stability, latency, tokens, and cost.
- Optional enterprise governance before a cluster, credential, or model is
  touched.
- Local audit and provenance evidence that can be verified after a run.

### The two modes

| Command | Use it when | Needs Kubernetes? |
|---|---|---:|
| `agent-eval review` | You want a pre-merge report for any Git repository | No |
| `agent-eval run` | You want to benchmark Claude Code or Codex on a coding task | Yes |

### What the outcomes mean

- **Accepted:** execution completed and every configured requirement passed.
- **Rejected:** execution completed, but correctness, safety, quality, or budget
  requirements failed.
- **Infrastructure error:** the harness cannot make a trustworthy product
  conclusion because execution or evidence collection failed.

A run can pass every hidden test and still be rejected if a required scanner is
missing, a secret is detected, or a budget is exceeded. A broken cluster is an
infrastructure error, not an agent-quality failure.

## Quick start

### Requirements

On macOS:

```sh
brew install k3d gitleaks trivy
```

You also need Docker, `kubectl`, [`uv`](https://docs.astral.sh/uv/), and at least
one authenticated agent backend:

- `ANTHROPIC_API_KEY` for Claude Code and the Claude judge.
- A logged-in Codex CLI for Codex: `codex login`.

Install the project and check the machine:

```sh
uv sync
uv run agent-eval doctor
```

### Review a pull request

No cluster is required:

```sh
uv run agent-eval review
uv run agent-eval review --base main --head my-branch
```

Run trusted project checks as part of the review:

```sh
uv run agent-eval review \
  --test-cmd "pytest -q" \
  --check "ruff check ." \
  --context @ticket.md \
  --allow-local-execution
```

`--allow-local-execution` is required whenever test, check, or generated-test
code will run. Those commands execute on the Mac, so only use the flag for a
change you trust.

Reports are written under `<repo>/.agent-eval/reviews/`:

```text
review.md      human-readable report
review.json    structured result
review.sarif   code-scanning result
```

The command exits with code 2 when risk is high or a blocking grader fails.

### Run a coding-agent evaluation

`run` creates or starts the local k3d cluster:

```sh
uv run agent-eval run --task example-todo-api --agent codex \
  --trials 3 --experiment-id todo-july-2026 --gate

uv run agent-eval run --task example-todo-api --agent claude-code \
  --trials 3 --experiment-id todo-july-2026 --gate
```

Compare and report the results:

```sh
uv run agent-eval compare --task example-todo-api --out comparison.json
uv run agent-eval report --task example-todo-api
uv run agent-eval verify-run --run <run-id>
```

Score a workspace produced elsewhere:

```sh
uv run agent-eval evaluate --task example-todo-api \
  --workspace /path/to/produced --gate
```

## How evaluation works

### Coding-agent path

1. The harness loads a versioned task with starter code, a prompt, hidden
   tests, resource limits, and acceptance rules.
2. A fresh, non-root agent pod receives the starter workspace but not the
   hidden tests.
3. Claude Code or Codex edits the workspace. The harness captures the transcript
   and any observable usage data.
4. The resulting workspace is copied out and the agent pod is removed.
5. A different, network-isolated evaluator pod receives the result and hidden
   tests.
6. Host-side scanners and an optional approved judge add more evidence.
7. The acceptance policy turns all evidence into an explicit outcome.
8. The run is saved in JSON and SQLite. Complete runs also receive local
   provenance evidence.

The fresh evaluator pod prevents agent-phase filesystem changes and background
processes from carrying into grading. It does not make the grader fully hostile-
code-proof: submitted code and hidden tests still share the evaluator process
and result volume.

### Pull-request path

1. Resolve the base and head Git states.
2. Load review policy from the trusted base, not from the proposed change.
3. Run scope rules, configured commands, tests, and scanners.
4. Screen external-model input for secrets before an AI call.
5. Require every AI finding to quote a real added or deleted diff line.
6. Recheck serious findings with an adversarial verification pass.
7. Produce one risk result with reasons, machine JSON, and SARIF.

An unverifiable AI finding remains visible in `review.json`, but it cannot raise
risk. Deterministic blocking evidence and confirmed blocker or major findings
can fail the review.

### Reviewer benchmark path

Reviewer quality is measured against an answer key. Matching is mechanical:
file, category, and line range must agree. Another model does not decide whether
the first model was correct.

The scorer reports:

- Precision, recall, and F1.
- Blocker and major recall.
- False positives per case and per KLoC.
- Clean-case accuracy and Wilson 95% intervals.
- Finding stability across repeated trials.
- Latency, tokens, cost, budget eligibility, and the efficiency frontier.

The checked-in demonstration can be reproduced without an LLM:

```sh
uv run agent-eval corpus validate benchmarks/reviewer-corpus/v1/corpus.yaml
uv run agent-eval benchmark-experiment \
  --experiment benchmarks/reviewer-corpus/v1/experiment.yaml \
  --out reviewer-experiment.json
```

The small fixture corpus proves the evaluation and CI path. It is not evidence
that one reviewer architecture is generally better than another.

## Review configuration

### Executable graders

| Grader | What it checks | Passing condition |
|---|---|---|
| Scope | Protected paths and diff size | Change stays within policy |
| Command | Build, lint, or typecheck commands | Exit code 0 |
| Classical | Test suite on the new code | Tests pass |
| Reverse-classical | New tests replayed against the base | Base suite passes, injected tests fail on base |
| Generated test | Model-written discriminating test | Passes on head and fails on base |
| Prompt review | Evidence-backed AI findings | Quoted evidence matches the named diff side |

Per-repository policy lives in `<repo>/.agent-eval.yaml`:

```yaml
review:
  test_cmd: "uv run pytest -q"
  checks:
    - "uv run ruff check ."
  blocked_paths:
    - ".github/workflows/*"
  allowed_paths: []
  max_files: 30
  max_lines: 800
  require_tests_for:
    - "src/*"
  required_scanners: [ruff, semgrep, gitleaks]
  max_lint_errors: 0
  max_security_findings_high: 0
  max_security_findings_medium: 0
  max_secrets: 0
  max_vulnerabilities: 0
```

Unknown policy keys are rejected. Required scanner evidence fails closed: an
unavailable scanner is not treated as zero findings.

### Benchmark any reviewer

`benchmark-review` accepts this project's `review.json` or a simple
`{"findings": [...]}` file for each case:

```yaml
cases:
  - id: auth-bypass
    changed_lines: 42
    expected:
      - id: AUTH-001
        severity: blocker
        category: security
        file: src/auth.py
        line_start: 81
        line_end: 86
  - id: clean-refactor
    changed_lines: 27
    expected: []
```

```sh
uv run agent-eval benchmark-review \
  --manifest benchmark.yaml \
  --reviews reviewer-outputs \
  --out benchmark-result.json \
  --min-precision 0.80 \
  --min-recall 0.75 \
  --min-critical-recall 0.90 \
  --max-fp-per-case 0.50 \
  --fail-on-missing
```

Missing output files and incomplete metadata remain visible and fail closed by
default. Use `--allow-missing` or `--allow-budget-failures` only for exploratory
analysis.

## Technical architecture

### Runtime stack

```text
macOS
└── Docker
    └── k3d
        └── k3s
            ├── agent pod
            └── evaluator pod
```

- **Docker image:** the repeatable task filesystem and toolchain.
- **k3d:** runs k3s nodes inside local Docker containers.
- **k3s:** supplies the Kubernetes API, scheduling, resource limits, Secrets,
  and NetworkPolicy.
- **Agent pod:** the disposable environment where the model edits code.
- **Evaluator pod:** a new environment where hidden tests grade the result.

Ordinary runs use an environment-context hash in the image tag and compare the
host Docker image ID with every k3d node. Governed runs use the stricter image
identity described below.

### Sandbox profile

Agent and evaluator pods:

- Run as a non-root UID.
- Use a read-only root filesystem and ephemeral writable volumes.
- Drop Linux capabilities and disable privilege escalation.
- Use `RuntimeDefault` seccomp.
- Receive no Kubernetes service-account token.
- Have CPU, memory, storage, and time bounds.

Evaluator pods deny all network egress. In proxy mode, agent pods have no
direct DNS or Internet path and can connect only to a per-trial Squid proxy.
The proxy resolves names and enforces provider-domain suffixes.

These are strong local guardrails, not a claim that a shared-kernel container
can safely contain fully malicious code.

### Scanner evidence

- Ruff runs as pinned `ruff==0.15.20` with `--isolated`.
- Semgrep runs as pinned `semgrep==1.169.0`, but `--config auto` still uses a
  mutable remote ruleset.
- Gitleaks and Trivy use the locally installed binaries and record their
  observed versions.
- The Trivy vulnerability database is not content-pinned.

For formal cross-machine or cross-time gates, pin scanner binaries, rulesets,
and vulnerability databases.

## Governed runs

Governance is optional. When enabled, it adds a strict request and policy gate
before cluster setup, image work, credential loading, or a model call.

Use the checked-in example:

```sh
uv run agent-eval run --task example-todo-api --agent claude-code \
  --model claude-sonnet-4-5-20250929 --trials 1 --gate \
  --governance-request examples/governance/request.yaml \
  --governance-policy examples/governance/policy.yaml
```

Both governance flags are required together. Unknown and duplicate YAML keys
are rejected.

### Admission

The request identifies:

- Tenant, project, actor, task, adapter, and exact model.
- Data classification and retention class.
- Optional token and cost limits.

The policy controls:

- Allowed tenants, projects, tasks, models, and judge identities.
- Data and retention classes.
- Network mode, proxy domains, and digest-pinned proxy images.
- Trial count, timeouts, scanner and judge phases.
- Credential-broker requirements and coding-agent budgets.

Model matching is exact. Prefixes and wildcards are not accepted. A governed
judge also requires scanning because gitleaks must screen its outbound input.

The effective coding-agent budget is the strictest limit from the policy,
request, registered model, and task acceptance contract. Missing required usage
evidence rejects rather than passing.

Judge spend is not included in the local budget ledger, and a limit does not
interrupt an in-flight provider generation. A production control plane still
needs provider limits and an atomic multi-trial ledger.

### Immutable execution identity

After preflight, the harness copies the admitted task into a private snapshot
and builds one Linux image. The final execution decision binds:

```text
content-derived image reference
+ single-platform manifest digest
+ Linux platform
```

The exact reference is imported into every k3d node. Governed agent and
evaluator pods use `imagePullPolicy: Never`, so a missing local image fails
instead of falling back to a registry. Runtime evidence must match the admitted
manifest.

The private snapshot prevents tag-cache substitution. It does not make an
untrusted Dockerfile safe or make the build hermetic. A production service
should use an isolated builder and promote signed digests through a trusted
registry.

### Exact model evidence

Claude Code exposes its runtime model in its event stream. The Anthropic judge
response also exposes the model that completed the request. Governed runs check
both against the exact admitted identity.

The checked-in Codex 0.144.4 JSON event schema does not expose runtime model
identity. Governed Codex coding and judging therefore fail closed instead of
trusting the requested command-line model. Codex subscription usage also lacks
per-request price evidence, so governed cost gates cannot accept it without a
trusted accounting integration.

### Audit and attestation

Each governed run records a content-minimized `audit.jsonl`. Events carry stage
names, IDs, statuses, counts, digests, and timing. They do not copy prompts,
source, transcripts, credentials, or command output. Every event hashes the
previous event.

Runs with complete provenance also receive an unsigned in-toto Statement
v1-shaped `attestation.json` and digest sidecar.

```sh
uv run agent-eval audit verify --run <run-id>
uv run agent-eval verify-run --run <run-id>
```

`verify-run` checks bounded no-follow file snapshots, artifact hashes, the task
tree, exact harness Git state, audit continuity, image and model identity,
governance replay, SQLite agreement, and the recomputed outcome.

This proves local consistency, not authorship. There is no signature, trusted
time, transparency log, WORM storage, or protection from a privileged user who
rewrites the complete unsigned bundle.

### Credentials

The fallback credential source is `ANTHROPIC_API_KEY` for Claude Code or
`~/.codex/auth.json` for Codex. The selected credential is copied through a
unique per-trial Kubernetes Secret.

For broker-minted credentials, set `AGENT_EVAL_CREDENTIAL_COMMAND` to an argv-
style command that returns:

```json
{
  "env": {"PROVIDER_TOKEN": "short-lived-value"},
  "files": {"codex-auth.json": "{...}"},
  "expires_at": "2026-07-13T22:15:00-07:00"
}
```

The expiry must cover the agent timeout plus 300 seconds. Secret values and
broker stdout are not written to errors or run records.

The evaluated agent can read whichever provider credential reaches its pod.
Use a narrowly scoped credential and dedicated test account for adversarial
tasks. A host crash can also leave Kubernetes objects until manual cleanup.

## Metrics and comparison

| Category | Evidence |
|---|---|
| Correctness | Hidden tests, command exit, coverage, resolved, pass@k |
| Efficiency | Wall time, tokens, cost, turns, tool calls |
| Quality | Ruff, Semgrep, Gitleaks, Trivy, diff size |
| Judge | Backend, observed model, rubric scores, rationale |
| Assurance | Challenge results, credential mode, proxy violations |
| Governance | Decisions, reason codes, identities, limits, policy digests |
| Outcome | Status, itemized checks, requirements, observed values |
| Provenance | Task tree, Git state, image identity, audit and artifact hashes |

Unobserved values remain `null`.

`compare` groups runs by adapter and recorded model. It reports sample size,
resolved rate with Wilson intervals, pass@k, infrastructure-failure rate,
acceptance rate, completeness, time, tokens, cost, judge scores, and diff
summaries.

Paired comparisons require the same experiment, task, trial number, task-tree
digest, and runtime image digest. Missing metrics do not become zero deltas.

## Enterprise controls and honest limits

Implemented controls include:

1. Versioned, fail-closed request and policy admission.
2. Exact coding and judge model identities when the provider exposes them.
3. Content-bound task snapshots and governed image identity.
4. Non-root, resource-bounded, network-controlled sandbox pods.
5. Secret screening before external model review.
6. Evidence-verified AI findings and trusted-base review policy.
7. Itemized outcomes that separate rejection from infrastructure failure.
8. Hash-locked reviewer fixtures with executable reproducers.
9. Content-minimized audit chains and locally verifiable provenance.
10. JSON and SARIF outputs for CI and code-scanning systems.

Important remaining boundaries:

- Submitted code and hidden tests still share the evaluator process.
- Review test and check commands run trusted change-controlled code on the Mac.
- Local attestations are unsigned.
- Scanner rules and vulnerability data are not fully immutable.
- Provider budgets are outcome gates, not generation-time interruption.
- Cleanup is best effort if the harness or host crashes.
- The seed reviewer corpus is too small for general model-ranking claims.
- Task Dockerfiles are trusted by the local host build path.

The next strong security boundary is protected out-of-process grading and
result capture. The next measurement boundary is a larger public PR corpus.

## Writing a task

```text
tasks/<task-id>/
├── task.yaml
├── environment/
│   ├── Dockerfile
│   └── workspace/
├── tests/
└── solution/
```

Rules:

- The directory and `task.yaml` ID must be the same lowercase DNS-style label.
- `test_command` runs from `/workspace`; hidden tests are mounted at `/tests`.
- Write JUnit XML to `/results/junit.xml` and optional coverage JSON to
  `/results/coverage.json`.
- Hidden tests should cover only the interface promised by the prompt.
- Declare every scanner required by acceptance.
- The image must include the selected agent CLI and `tar`, and support the
  configured non-root UID.
- `agent-eval tasks validate <id>` requires the starter to fail and the oracle
  solution to pass through the real evaluator.

Default resources:

```yaml
resources:
  agent:
    requests: {cpu: "100m", memory: "128Mi", ephemeral-storage: "256Mi"}
    limits: {cpu: "2", memory: "2Gi", ephemeral-storage: "4Gi"}
  eval:
    requests: {cpu: "100m", memory: "128Mi", ephemeral-storage: "256Mi"}
    limits: {cpu: "2", memory: "2Gi", ephemeral-storage: "4Gi"}
```

Task acceptance and sandbox policy live in `task.yaml`:

```yaml
acceptance:
  min_coverage_percent: 85
  min_judge_score: 3.5
  required_scanners: [ruff, semgrep, gitleaks]
  max_lint_errors: 0
  max_security_findings_high: 0
  max_secrets: 0
  max_wall_time_s: 600
  max_total_tokens: 100000
  max_cost_usd: 2.00
  require_challenges_passed: true

network:
  agent_mode: proxy
  allowed_domains: []
  proxy_image: ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029

security:
  run_as_non_root: true
  run_as_user: 10001
  run_as_group: 10001
  read_only_root_filesystem: true
```

Once a threshold is present, missing evidence fails closed.

## Adding an agent adapter

Implement `AgentAdapter` in `src/agent_eval/agents/base.py`:

1. Produce a shell command that runs the agent against the prompt file.
2. Request a machine-readable transcript on stdout.
3. Parse the transcript into `AgentMetrics`.
4. Register the adapter in `src/agent_eval/agents/__init__.py`.

## Configuration

- `AGENT_EVAL_JUDGE`: `claude`, `codex`, or `auto`.
- `AGENT_EVAL_JUDGE_MODEL`: judge model override for an unpinned task.
- `AGENT_EVAL_CREDENTIAL_COMMAND`: credential broker command.
- `--model` on `run`: coding-agent model override.

A task-level `judge.backend` and `judge.model` pair takes precedence. Governed
runs require an exact task-pinned judge identity and approved registry entry.

## Project layout

| Path | Responsibility |
|---|---|
| `src/agent_eval/cli.py` | Commands and CI exit behavior |
| `src/agent_eval/runner.py` | Coding-agent and evaluator pipeline |
| `src/agent_eval/kube.py` | Pod manifests and Kubernetes operations |
| `src/agent_eval/review.py` | Pull-request evidence and risk calculation |
| `src/agent_eval/review_benchmark.py` | Gold-label reviewer scoring |
| `src/agent_eval/review_experiment.py` | Repeated single and panel experiments |
| `src/agent_eval/governance.py` | Request, policy, registry, and decisions |
| `src/agent_eval/audit.py` | Lifecycle events and hash-chain verification |
| `src/agent_eval/attestation.py` | Local provenance creation and verification |
| `src/agent_eval/outcome.py` | Fail-closed task acceptance |
| `src/agent_eval/task.py` | Task, resources, security, and rubric schema |
| `benchmarks/reviewer-corpus/v1` | Executable reviewer fixtures |
| `tasks/` | Coding-agent tasks and oracle solutions |

## Design influences

The July 14, 2026 design review adapted patterns from pinned revisions of
[Harbor](https://github.com/harbor-framework/harbor/tree/d8c3140be1a0d7f4d2cb164fc7011dce40d3f0d8),
[Terminal-Bench](https://github.com/harbor-framework/terminal-bench/tree/d28711d0da2675d0bb1d56de45ae5df6082438a3),
[SWE-bench](https://github.com/princeton-nlp/SWE-bench/tree/f7bbbb2ccdf479001d6467c9e34af59e44a840f9),
[OpenHands](https://github.com/OpenHands/OpenHands/tree/5f9906fbdac3b30af7afa582af8845064dd43fc6),
and [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai).

Additional primary references:

- [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md)
- [Docker Buildx](https://docs.docker.com/reference/cli/docker/buildx/build/)
- [Docker image digests](https://docs.docker.com/dhi/core-concepts/digests/)
- [Kubernetes images](https://kubernetes.io/docs/concepts/containers/images/)
- [Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/)
- [OpenTelemetry semantic conventions](https://opentelemetry.io/docs/specs/semconv/)
- [OPA decision logs](https://www.openpolicyagent.org/docs/management-decision-logs)
- [Sigstore blob signing](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/)
- [SLSA 1.2](https://slsa.dev/spec/v1.2/)
- [GitHub SARIF integration](https://docs.github.com/en/code-security/concepts/code-scanning/sarif-files)

This repository uses these patterns as design input. It does not claim wire
compatibility, signed provenance, SLSA compliance, or general model-safety
proofs.
