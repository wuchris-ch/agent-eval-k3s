# agent-eval-k3s

Evaluate code changes and coding agents with evidence, not just model opinions.

## Overview

`agent-eval-k3s` is a local-first assurance harness for software engineering
teams. It answers two practical questions:

1. **Should this pull request merge?** `agent-eval review` checks the diff with
   policy rules, commands, tests, scanners, and evidence-verified AI findings.
2. **How well did this coding agent perform?** `agent-eval run` gives the agent
   a repeatable task in Kubernetes, grades the result against hidden tests, and
   saves a comparable scorecard.

Both paths use the same principle: important claims need machine-checkable
evidence. Missing evidence stays missing. It is never silently converted to a
clean result or a zero.

![Overview of the pull-request review and coding-agent evaluation workflows](docs/agent-eval-overview.svg)

### What you get

- Explicit `accepted`, `rejected`, or `infra_error` outcomes for agent trials.
- Low, medium, or high risk reports for pull requests, plus JSON and SARIF.
- Hidden-test correctness, scanner findings, coverage, latency, token use,
  cost, tool use, diff size, and judge results when those values are observable.
- Versioned, queryable assessments with exact dataset and evaluator identity.
- Repeatable reviewer benchmarks with precision, recall, F1, false positives,
  stability, latency, tokens, and cost.
- Optional enterprise governance before a cluster, credential, or model is
  touched.
- Local audit and provenance evidence that can be verified after a run.
- Optional, content-minimized OpenTelemetry export to any OTLP-compatible
  backend.

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

Clone the project, then install its external tools and Python environment:

```sh
git clone https://github.com/wuchris-ch/agent-eval-k3s.git
cd agent-eval-k3s

brew install uv kubectl k3d gitleaks trivy
uv sync
```

You also need Docker and at least one authenticated agent backend:

- `ANTHROPIC_API_KEY` for Claude Code and the Claude judge.
- A logged-in Codex CLI for Codex: `codex login`.

Check which features the machine is ready to use:

```sh
uv run agent-eval doctor
```

`uv sync` installs `agent-eval` and its locked Python dependencies into the
project's local `.venv`. Run the commands below from the `agent-eval-k3s`
checkout so `uv run` uses that environment.

### Review a pull request

No cluster is required. Pass the Git repository you want to review with
`--repo`:

```sh
uv run agent-eval review --repo /path/to/repository
uv run agent-eval review --repo /path/to/repository \
  --base main --head my-branch
```

If `--repo` is omitted, `agent-eval` reviews the current directory. If `--head`
is omitted, it reviews the target repository's current working-tree changes.

Run trusted project checks as part of the review:

```sh
uv run agent-eval review \
  --repo /path/to/repository \
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
4. Remaining agent processes are stopped. Only after three clean process scans
   is the resulting workspace copied out and the agent pod removed.
5. In `isolated-black-box` mode, a submission pod receives only the produced
   workspace and starts the declared service. A separate evaluator pod receives
   only the hidden tests and result volume. The evaluator can reach one declared
   submission TCP port through an exact directional NetworkPolicy pair.
6. Host-side scanners and an optional approved judge add more evidence.
7. The acceptance policy turns all evidence into an explicit outcome.
8. The run is saved in JSON and SQLite. Complete runs also receive local
   provenance evidence.

Governed runs require `isolated-black-box` mode and reject the legacy
`cooperative` mode before cluster, credential, or model side effects. Submitted
code cannot read the evaluator's hidden-test or result volumes, and hidden
tests do not import or execute the submitted workspace in their process. They
exercise only the task's declared HTTP interface through
`AGENT_EVAL_SUBMISSION_URL`.

This is a protected grader boundary, not a complete hostile-code sandbox. The
submission and evaluator pods still run on the same local k3s worker and, unless
an operator configures a hardened RuntimeClass, share its kernel. Ordinary
trusted tasks may retain `cooperative` mode for compatibility, where the
workspace and hidden tests do share one evaluator pod.

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

An explicit `--head` is reviewed from resolved commit objects. Static diff and
file collection do not check out that ref or run repository hooks, external
diff drivers, text conversion, or content filters. Local test and check
commands remain a separate, explicit `--allow-local-execution` boundary.

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

Static validation does not execute corpus-controlled commands. It is the
default; scripts can pass `--no-execute` to make the choice explicit:

```sh
uv run agent-eval corpus validate benchmarks/reviewer-corpus/v1/corpus.yaml
```

Execute base/head reproducers only for a trusted corpus. This runs local code
with your user's filesystem permissions, using a minimal environment that does
not forward host credentials:

```sh
uv run agent-eval corpus validate \
  benchmarks/reviewer-corpus/v1/corpus.yaml \
  --allow-local-execution
uv run agent-eval benchmark-experiment \
  --experiment benchmarks/reviewer-corpus/v1/experiment.yaml \
  --out reviewer-experiment.json
```

Corpus schema 1.0 binds each case's base tree, head tree, canonical diff,
labels, reproducer polarity, and artifact hashes. Validation works from a
bounded private snapshot and fails if an opt-in reproducer mutates that
snapshot. Reviewer experiment schema 2 pins the exact benchmark SHA-256 and
rejects output reuse across systems or trials. Incomplete single or panel
outputs stay incomplete and do not contribute quality scores.

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

Governed isolated black-box runs use this stack:

```text
macOS
└── Docker
    └── k3d
        └── k3s
            ├── agent pod
            └── isolated evaluation
                ├── submission pod (produced workspace only)
                └── evaluator pod (hidden tests and results only)
```

- **Docker image:** the repeatable task filesystem and toolchain.
- **k3d:** runs k3s nodes inside local Docker containers.
- **k3s:** supplies the Kubernetes API, scheduling, resource limits, Secrets,
  and NetworkPolicy.
- **Agent pod:** the disposable environment where the model edits code.
- **Submission pod:** the disposable target that runs the produced workspace in
  isolated black-box evaluation.
- **Evaluator pod:** the trusted environment that owns hidden tests and results
  and reaches only the submission's declared port.

Ordinary runs use an environment-context hash in the image tag and compare the
host Docker image ID with every k3d node. Governed runs use the stricter image
identity described below.

### Sandbox profile

Agent, submission, and evaluator pods:

- Run as a non-root UID.
- Use a read-only root filesystem and ephemeral writable volumes.
- Drop Linux capabilities and disable privilege escalation.
- Use `RuntimeDefault` seccomp.
- Receive no Kubernetes service-account token.
- Have CPU, memory, storage, and time bounds.

The namespace enforces the Kubernetes `restricted` Pod Security Standard at
the pinned k3s v1.35 API level and has bounded pod, object, CPU, memory, and
ephemeral-storage quotas. Set `AGENT_EVAL_RUNTIME_CLASS` to a preinstalled
RuntimeClass such as a reviewed gVisor or Kata configuration to apply that
runtime to agent, submission, evaluator, and egress-proxy pods. The default k3d
cluster does not install a hardened RuntimeClass.

Evaluator and submission pods deny external network egress. During isolated
black-box grading, additive policies allow only evaluator-to-submission TCP on
the declared port. The harness uses the target Pod IP directly, without a
Service or DNS grant. In proxy mode, agent pods have no direct DNS or Internet
path and can connect only to a per-trial Squid proxy. The proxy resolves names,
enforces provider-domain suffixes, and rejects local, private, link-local,
metadata, multicast, and reserved destination addresses.

These are strong local guardrails, not a claim that a shared-kernel container
can safely contain fully malicious code.

### Scanner evidence

The frozen scanner lock currently has one documented, time-limited
[PYSEC-2026-2132 reachability exception](https://github.com/wuchris-ch/agent-eval-k3s/blob/main/docs/security-exceptions/PYSEC-2026-2132.md).
CI verifies its exact Semgrep and Click versions, source archive digest, and
2026-08-14 review deadline while continuing to block every other advisory.

- Ruff and Semgrep run through the bundled Python 3.12 scanner project with
  `uv run --frozen --offline`. The wheel contains the exact project, complete
  transitive lock, and local scanner configuration. Separate SHA-256 values
  bind the complete runtime bundle, project, lockfile, scanner invocation
  policy, Semgrep ruleset, and Gitleaks configuration.
- Ruff runs as exact `ruff==0.15.20` with `--isolated`, `--ignore-noqa`, and
  `--no-respect-gitignore`. A bounded no-follow inventory passes every
  classified Python source as an explicit target with `--no-force-exclude`,
  including sources under `.venv` and other normally excluded directories.
  Unknown-extension UTF-8 text that parses as Python is included, so moving a
  Python entry point to a `.txt` file does not suppress Ruff or Semgrep.
  Target-controlled Ruff configuration, inline `noqa` comments, Git ignore
  rules, default directory exclusions, and ordinary extension filtering cannot
  suppress those classified sources.
- Semgrep runs as exact `semgrep==1.169.0` with metrics disabled and the
  first-party Python security baseline packaged in `semgrep.yml`. It does not
  fetch registry rules or perform a version check during a scan. The pinned
  invocation disables inline `nosem` suppression, Git ignore filtering,
  `.semgrepignore` filtering, binary detection, and the default target-size
  cutoff. It receives that same explicit source inventory with
  `--scan-unknown-extensions`, and the reported scanned-path set must cover
  every target. Semgrep report errors, skipped rules, reported skipped targets,
  and missing target coverage fail closed instead of producing partial
  security metrics.
- Scanner processes receive a credential-minimized environment. Host API keys
  and cloud credentials are not inherited; only the executable path,
  certificate and unauthenticated proxy settings are retained. HOME, temporary
  files, caches, and the `uv` environment are private and keyed by the bundled
  project and lock SHA-256.
- Every scan binds a full-tree SHA-256 for the prepared Python environment and
  executable SHA-256 values for `uv`, Python, Ruff, Semgrep, Gitleaks, and
  Trivy. This covers installed Python package files rather than trusting only
  launcher scripts. Gitleaks' embedded rules are bound to its executable
  identity, and its packaged configuration forces those defaults so a target
  `.gitleaks.toml` cannot suppress findings. Gitleaks scans a private, bounded,
  no-follow mirror that recursively neutralizes scanner-native `.git` and
  `node_modules` skip names, treats target `.gitleaksignore` files as ordinary
  scan data, maps findings back to their original paths, disables
  `gitleaks:allow` comments, and sets the file-size cutoff to unlimited. The
  mirror is removed before redacted results are persisted.
- Trivy disables database updates during the scan. Its identity includes
  bounded database metadata and an exact SHA-256 over the local database file
  names and bytes. Any database change creates a different scanner identity.
  Both `--config` and `--ignorefile` point to the hash-bound evaluator-owned
  empty policy, so caller or target `trivy.yaml` and `.trivyignore` files cannot
  suppress vulnerability results. Trivy scans the same kind of bounded
  no-follow mirror with recursive native skip-name neutralization, and its
  database metadata and content identity must be unchanged across the scan.

Prepare the private environment and Trivy database explicitly before governed
or otherwise promotion-grade scans:

```sh
uv run agent-eval scanners prepare
uv run agent-eval scanners identity
```

`prepare` performs a bounded frozen sync, downloads the Trivy vulnerability
database when the separately installed Trivy binary is present, prints the
resulting identity, and exits nonzero until the stack is promotion-ready.
Gitleaks and Trivy binaries must be installed separately. `identity` is
read-only and never downloads materials. Actual Ruff and Semgrep scans use
`--frozen --offline --no-sync`, and Trivy uses `--skip-db-update`, so evaluation
never mutates the admitted package or database inputs. Missing artifacts remain
unavailable evidence. No `.venv` is written into the source checkout or
installed package. Runtime state is stored beside the configured application
state directory as `<state-name>-scanner-runtime/`.

CI verifies checksum-pinned Gitleaks 8.30.1 and Trivy 0.72.0 archives, scans the
repository for secrets with a narrow fixture-hash allowlist, and requires a
promotion-ready four-scanner smoke run. Its real-binary adversarial checks also
prove that inline and file-based Gitleaks suppression, default `.git` skipping,
size cutoffs, and Trivy ignore/config files do not hide the pinned fixtures.

A scanner preflight creates one canonical identity over all of those inputs and
marks it promotion-ready only when every required scanner reports the exact
supported version and every executable and Trivy database content digest is
complete. A governed policy must allowlist
that exact identity. The completed scan recomputes the identity and the run
fails closed if readiness or identity differs from admission. The bundled Ruff
and Semgrep rules currently provide a Python-focused baseline; broader
language-specific scanner coverage remains future work.

## Governed runs

Governance is optional. When enabled, it adds a strict request and policy gate
before cluster setup, image work, credential loading, or a model call.

The checked-in governance files are schema templates, not runnable production
policy. Before use, replace every placeholder scanner identity, task-tree and
execution digest, image reference and manifest digest, builder assertion,
source revision, and provenance digest with reviewed values from your own
promotion process. The all-zero and all-`f` values are deliberately invalid as
operational approvals.

After replacing those placeholders, a governed command has this shape:

```sh
uv run agent-eval run --task example-todo-api --agent claude-code \
  --model claude-sonnet-4-5-20250929 --trials 1 --gate \
  --governance-request examples/governance/request.yaml \
  --governance-policy examples/governance/policy.yaml
```

Both governance flags are required together. Unknown and duplicate YAML keys
are rejected. New requests use `agent-eval.request/v2` and the explicit
`max_observed_total_tokens` and `max_observed_cost_usd` names. The loader accepts
strict historical v1 requests with `max_total_tokens` and `max_cost_usd` and
normalizes them visibly to v2; it does not accept a mixture of schemas.

Policy v1 is not normalized because it lacks security-critical approvals that
cannot be inferred. To migrate, create an `agent-eval.policy/v2` document, add
an exact `task_registry` with task-tree and execution-recipe digests, add a
reviewed `approved_images` entry for every permitted platform, rename policy
and model limits to `max_observed_total_tokens` and
`max_observed_cost_usd`, and populate `allowed_scanner_identities` from a
promotion-ready `agent-eval scanners identity` result. The loader emits an
explicit migration error for policy v1 instead of manufacturing approvals.

### Admission

The request identifies:

- Tenant, project, actor, task, adapter, and exact model.
- Data classification and retention class.
- Optional post-run observed token and cost thresholds.

The policy controls:

- Allowed tenants and projects, plus an exact task registry that binds the task
  tree and each approved execution-specification digest.
- Exact coding-model and judge-model identities.
- Data and retention classes.
- Network mode, proxy domains, and digest-pinned proxy images.
- Trial count, timeouts, scanner and judge phases.
- Credential-broker requirements and post-run observed coding-agent
  thresholds.
- Exact promotion-ready scanner identities when scans are required.

Model matching is exact. Prefixes and wildcards are not accepted. A governed
judge also requires scanning because gitleaks must screen its outbound input.

The admitted governance threshold is the strictest value from the policy,
request, and registered model. Outcome evaluation then intersects it with any
stricter task acceptance threshold. Missing required usage evidence rejects
rather than passing. After each governed trial, the CLI checks that trial's
observed tokens and cost against the admitted governance thresholds. Missing
evidence or an exceeded threshold stops the CLI before it starts the next
trial.

These are outcome and next-trial gates. They do not reserve spend, aggregate an
atomic ledger across processes, or interrupt an in-flight provider generation.
Judge spend is not included. A production control plane still needs provider
limits, reservations, and an atomic tenant ledger for hard budget enforcement.

### Immutable execution identity

Generate the task-registry entry for one recipe, or every supported scanner and
judge switch combination:

```sh
uv run agent-eval tasks fingerprint example-todo-api --scan --judge
uv run agent-eval tasks fingerprint example-todo-api --all-recipes
```

The execution digest covers the complete task manifest and tree, grader
switches, scanner runtime, configured RuntimeClass, and exact k3s image digest.
Changing any of those inputs requires an explicit registry update. Policy
schema v2 requires a task registry and rejects unregistered, changed, retired,
or suspended tasks before image import or credential access. The fingerprint
command prints task and execution-recipe digests only. It neither builds nor
approves an image.

Each task-registry entry must separately preapprove one image for every allowed
Linux platform. The approval binds the reference, single-platform manifest
digest, builder ID, build type, source revision, and provenance SHA-256. Image
construction and promotion happen outside governed evaluation. After preflight,
the harness copies the admitted task into a private snapshot and selects only
the image approved for the Docker server platform. The final execution decision
binds:

```text
content-derived image reference
+ single-platform manifest digest
+ Linux platform
```

The local Docker manifest must already match the approval. The exact reference
is imported into every k3d node, and the agent, submission, and evaluator pods
use `imagePullPolicy: Never`. A missing image fails instead of triggering a
runtime build or registry fallback. Runtime evidence from both isolated grading
pods must match the admitted manifest. On each pinned k3s node, verification
hashes the exact containerd reference target. If Docker imported an OCI index,
it selects exactly one real Linux child whose digest equals the expected Docker
platform manifest, hashes that child, and ignores non-platform attestations.
The child's config digest must be both the CRI image config and the running
pod's image ID. This does not depend on optional CRI `repoDigests` metadata.

The private snapshot and runtime checks prevent tag-cache substitution inside
the local workflow. Builder and provenance fields are policy assertions, not a
cryptographic verification of a registry artifact. A production service still
needs an isolated builder, signed policy and provenance, and authenticated
digest promotion through a trusted registry.

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
broker stdout are not written to errors or run records. Broker output,
credential value counts and sizes, JSON depth, and redaction material are
bounded. The broker runs in a private process group that is terminated after
the result, including descendants that remain in that group. The broker command
is trusted operator configuration, not a sandbox for hostile native code; a
process that deliberately creates a new session can escape portable process-
group cleanup. Run untrusted broker implementations inside a separately
supervised service or hardened worker boundary.

Before a durable write, the runner redacts the exact projected environment
values, complete projected auth files, sensitive JSON leaves, and JSON-escaped
forms from transcripts, stderr, proxy logs, and run records. The returned
workspace first lands in an owner-only temporary directory. Bounded no-follow
inspection covers regular-file contents and relative path names; a credential
hit deletes the staged snapshot and blocks evaluation. A final containment gate
rejects late derived artifacts before persistence or attestation.

The evaluated agent can read whichever provider credential reaches its pod.
Use a narrowly scoped credential and dedicated test account for adversarial
tasks. Exact containment cannot recognize a deliberately fragmented, encrypted,
hashed, or independently encoded credential. Environments that require that
control need a separate DLP boundary. Secret creation retains its generated
name and confirms rollback after an ambiguous API error; an unconfirmed cleanup
reports the exact credential-free `kubectl` remediation command. A host crash
can still leave Kubernetes objects until manual cleanup.

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

Every completed run also projects deterministic tests, scanners, model judges,
adversarial challenges, governance, and the final outcome into one strict
`agent-eval.assessment/v1` envelope. Assessments include typed values, status,
direction, thresholds, evaluator identity and configuration digests, and the
task's dataset revision and item ID. SQLite stores them in a normalized table
for filtering without parsing result blobs. Prompts, source, diffs, rationales,
commands, and transcripts are excluded from this envelope.

`compare` groups runs by adapter and recorded model. It reports sample size,
resolved rate with Wilson intervals, pass@k, infrastructure-failure rate,
acceptance rate, completeness, time, tokens, cost, judge scores, and diff
summaries.

Paired comparisons require the same experiment, task, trial number, task-tree
digest, and runtime image digest. Missing metrics do not become zero deltas.

Aggregation also requires an exact match on evaluation-specification digest,
task-tree digest, runtime image digest, harness version, complete Git commit and
worktree identity. The evaluation-specification digest binds the configured
evaluator recipe and dataset identity before execution. Observed assessment
availability and evaluator identity remain result evidence and never change a
run's cohort or denominator. Third-party adapter runs also need distribution,
version, and installed-artifact identity. Runs missing any required binding are
shown as distinct legacy-unbound single-run cohorts and are never paired or
pooled.

## Enterprise controls and honest limits

Implemented controls include:

1. Versioned, fail-closed request and policy admission.
2. Exact coding and judge model identities when the provider exposes them.
3. Governed isolated black-box grading with separate submission and evaluator
   pods, directional network policy, and evaluator-owned results.
4. Content-bound task snapshots and preapproved per-platform task images.
5. Frozen, offline scanner recipes with exact executable and Trivy database
   identity, policy allowlisting, and post-scan identity verification.
6. Non-root, resource-bounded, network-controlled sandbox pods.
7. Secret screening before external model review.
8. Evidence-verified AI findings and trusted-base review policy.
9. Itemized outcomes that separate rejection from infrastructure failure.
10. Hash-locked reviewer fixtures with executable reproducers.
11. Content-minimized audit chains and locally verifiable provenance.
12. JSON and SARIF outputs for CI and code-scanning systems.

Important remaining boundaries:

- Submission and evaluator pods still share a local worker kernel unless a
  reviewed gVisor, Kata, or other hardened RuntimeClass is configured.
- Review test and check commands run trusted change-controlled code on the Mac.
- Local policy, image-builder assertions, audit bundles, and attestations are
  unsigned. The harness does not verify registry signatures, trusted time, or a
  transparency log.
- Governed token and cost controls are observed outcome and next-trial gates,
  not provider-side reservations or atomic generation-time limits.
- Ruff and the packaged Semgrep baseline are Python-focused. Equivalent
  language-specific lint and static-analysis profiles are not yet bundled for
  JavaScript, TypeScript, Go, Rust, Java, or other ecosystems.
- Operators must run the explicit scanner preparation and identity-promotion
  workflow before offline governed scans. Evaluation does not fetch missing
  packages or vulnerability data.
- Cleanup is best effort if the harness or host crashes.
- The seed reviewer corpus is too small for general model-ranking claims.
- Ordinary, non-governed task Dockerfiles are trusted by the local host build
  path. Governed execution consumes a preapproved image but still trusts the
  unsigned policy assertion that describes its builder and provenance.
- SQLite is a single-user workstation store, not a multi-tenant production
  control-plane database.

The next strong security boundaries are hardened workers, signed policy and
registry verification, and an authenticated multi-tenant control plane. The
next measurement boundary is a larger reviewed public PR corpus.

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

- Declare `schema_version: agent-eval.task/v1` and an exact task `version`.
- Manifests created before task schemas existed remain readable with the
  explicit `legacy-unversioned` binding. A manifest that declares only one of
  `schema_version` or `version` is rejected.
- When a task belongs to a dataset, declare exact `dataset.id`,
  `dataset.revision`, and `dataset.item_id` values.
- The directory and `task.yaml` ID must be the same lowercase DNS-style label.
- Remove generated host state before fingerprinting. Task loading rejects
  Python bytecode, `__pycache__`, common Python tool caches, coverage state,
  and `.DS_Store` so those files cannot alter an approval or enter an image
  context accidentally.
- Governed tasks must use `evaluation.mode: isolated-black-box` and declare a
  bounded submission command, TCP port, and HTTP readiness path. Cooperative
  mode is available only for ordinary compatibility runs with trusted tasks.
- In isolated mode, hidden tests are mounted only into the evaluator pod and
  must exercise the documented remote interface through
  `AGENT_EVAL_SUBMISSION_URL`. They must not import the submitted workspace.
- `test_command` runs in the evaluator pod; hidden tests are mounted at
  `/tests`.
- Write JUnit XML to `/results/junit.xml` and optional coverage JSON to
  `/results/coverage.json`.
- Do not set a coverage acceptance threshold for black-box tasks unless the
  evaluator has a separately trusted way to measure target coverage. Coverage
  of the hidden-test client is not submission coverage.
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
schema_version: agent-eval.task/v1
version: 1.0.0
id: example-todo-api
prompt: Implement the approved todo API change without modifying evaluator controls.
dataset:
  id: company/engineering-evals
  revision: 2026-07-14.1
  item_id: example-todo-api

evaluation:
  mode: isolated-black-box
  submission_command: python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
  submission_port: 8080
  readiness:
    path: /openapi.json
    timeout_seconds: 30

test_command: >-
  python -m pytest /tests -q
  --junitxml=/results/junit.xml

acceptance:
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

Built-in adapters remain reserved. A third-party distribution registers an
adapter class, instance, or zero-argument factory with this entry point:

```toml
[project.entry-points."agent_eval.agents"]
my-agent = "my_agent.adapter:MyAdapter"
```

The adapter must implement the `AgentAdapter` surface from
`src/agent_eval/agents/base.py`:

1. Produce a shell command that runs the agent against the prompt file.
2. Request a machine-readable transcript on stdout.
3. Parse the transcript into `AgentMetrics`.
4. Expose a string-to-string `env` mapping and, optionally, callable `prepare`.

The entry-point name and adapter `name` must match and use a lowercase DNS
label of at most 63 characters. Plugins cannot use the reserved `codex` or
`claude-code` names. Environment keys must be valid variable names, and static
`env` values must not embed secrets. Third-party adapter credentials require
`AGENT_EVAL_CREDENTIAL_COMMAND`.

`get_adapter(name)` imports and initializes only the selected plugin.
`list_adapters()` returns built-in and installed entry-point metadata without
importing plugin modules, and marks invalid, duplicate, or shadowing entries as
unavailable. Selected plugins execute in the host process, so install only
reviewed and trusted distributions.

Governed runs currently accept built-in adapters only. A third-party entry
point is executable host code, and this policy schema does not bind its
distribution version, installed-file digest, or signature. The CLI rejects it
before entry-point discovery or import rather than pretending an adapter name
is an artifact identity. Ordinary plugin runs without that exact artifact
identity remain usable as individual evidence but are never pooled or paired
by `compare`.

## Local state and task discovery

Run records no longer default to a checkout-relative `runs/` directory. On
macOS they live at:

```text
~/Library/Application Support/agent-eval
```

Inspect the active path and safely migrate an existing checkout-local store:

```sh
uv run agent-eval state path
uv run agent-eval state migrate --from ./runs
uv run agent-eval state migrate --from ./runs --apply
```

Migration is a dry run unless `--apply` is present. It rejects links and
special files, pins the source root and traverses every component through
no-follow directory descriptors, and detects changes using inode, full mode,
size, mtime, ctime, and link-count evidence. It reconciles SQLite rows with
their run artifacts and applies additive schema migrations to a private copy.
The destination must not exist, so cutover is one same-filesystem atomic
directory rename and no existing state is ever replaced. The source is never
deleted. If parent-directory `fsync` reports a durability failure after the
rename, the destination still contains only the complete validated tree and
the source remains available for recovery.

Set `AGENT_EVAL_STATE_DIR` before starting the process to use a different state
root. Set `AGENT_EVAL_TASKS_DIR` to a directory containing organization-owned
tasks. Source checkouts also discover the repository's `tasks/` directory. The
Python wheel intentionally contains no benchmark tasks, so wheel installations
need `AGENT_EVAL_TASKS_DIR` for `run`, `evaluate`, and task commands.

## OpenTelemetry export

Canonical JSON, SQLite, audit, and attestation records remain authoritative.
Telemetry is an optional, lossy projection and an exporter failure never
changes the saved outcome.

Install the optional SDK and enable export explicitly:

```sh
uv sync --extra observability
export AGENT_EVAL_OTEL_ENABLED=1
export OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
```

`http/protobuf` is also supported through
`OTEL_EXPORTER_OTLP_PROTOCOL` or `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`.
Standard OpenTelemetry authentication headers remain exporter configuration.
Only allowlisted low-cardinality run attributes and content-free assessment
events are emitted. Set `AGENT_EVAL_ENVIRONMENT` to one of `development`,
`staging`, `production`, or `test`. `AGENT_EVAL_OTEL_FLUSH_TIMEOUT_MS` is
bounded from 100 to 10000 milliseconds and defaults to 3000.

## Configuration

- `AGENT_EVAL_STATE_DIR`: local JSON, SQLite, audit, and attestation root.
- `AGENT_EVAL_TASKS_DIR`: organization-owned task directory.
- `AGENT_EVAL_RUNTIME_CLASS`: preinstalled Kubernetes RuntimeClass applied to
  all evaluation workloads and included in governance fingerprints.
- `AGENT_EVAL_JUDGE`: `claude`, `codex`, or `auto`.
- `AGENT_EVAL_JUDGE_MODEL`: judge model override for an unpinned task.
- `AGENT_EVAL_CREDENTIAL_COMMAND`: credential broker command.
- `AGENT_EVAL_QUOTA_*`: bounded namespace object and compute quota overrides;
  see `src/agent_eval/kube.py` for names and hard maximums.
- `AGENT_EVAL_OTEL_ENABLED`: explicit opt-in for the OTLP projection.
- `AGENT_EVAL_ENVIRONMENT`: bounded deployment environment telemetry value.
- `AGENT_EVAL_OTEL_FLUSH_TIMEOUT_MS`: bounded best-effort flush timeout.
- `--model` on `run`: coding-agent model override.

A task-level `judge.backend` and `judge.model` pair takes precedence. Governed
runs require an exact task-pinned judge identity and approved registry entry.

## Project layout

| Path | Responsibility |
|---|---|
| `src/agent_eval/cli.py` | Commands and CI exit behavior |
| `src/agent_eval/runner.py` | Coding-agent and evaluator pipeline |
| `src/agent_eval/kube.py` | Pod manifests and Kubernetes operations |
| `src/agent_eval/assessments.py` | Normalized, content-minimized assessment schema |
| `src/agent_eval/observability.py` | Optional privacy-safe OpenTelemetry projection |
| `src/agent_eval/paths.py` | Portable task discovery and private local state paths |
| `src/agent_eval/state.py` | Validated legacy-state migration |
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

The complete landscape review, architecture decision, verified project
versions, primary sources, and remaining production roadmap are in
[`docs/enterprise-direction-2026-07-14.md`](docs/enterprise-direction-2026-07-14.md).

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
