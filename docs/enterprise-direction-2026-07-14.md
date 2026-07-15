# Enterprise direction: evaluation assurance, not another dashboard

Status: accepted architecture decision

Research cutoff: July 14, 2026, America/Vancouver

## Decision

`agent-eval-k3s` will be an evidence-grade assurance layer for coding-agent and
code-review evaluations. It will not become a proprietary tracing dashboard or
a general agent framework.

The system owns the parts that must be independently verifiable:

- Exact task, dataset, execution recipe, model, image, policy, and evaluator
  identity.
- Isolated execution and explicit trust boundaries.
- Normalized assessments and fail-closed outcomes.
- Tamper-evident, content-minimized audit and local provenance.
- Benchmark quality checks, reproducibility, cohort integrity, and statistical
  comparison.

OpenTelemetry is the interoperability boundary for operational telemetry.
Phoenix, Langfuse, MLflow, Weave, Opik, Tempo, and similar systems remain
optional destinations. Telemetry is never the authoritative audit record and a
telemetry outage must not change an evaluation outcome.

## Landscape reviewed

The review covered leading open-source evaluation harnesses, coding-agent
benchmarks, evaluation libraries, and observability systems. Version and
activity checks used project release pages and repositories on the research
cutoff date.

| Area | Projects reviewed | Pattern adopted here |
|---|---|---|
| General eval harnesses | Inspect AI 0.3.246, Inspect Evals 0.14.4, Harbor 0.18.0, Pydantic Evals 2.10.0, OpenAI Evals | Composable tasks, explicit solvers and graders, eval sets, durable logs, plugin boundaries |
| Coding-agent evals | SWE-bench 4.1.0, SWE-agent 1.1.0, Terminal-Bench, LiveCodeBench, SWE-Lancer, STATE-Bench, tau2-bench 2.3.3 | Containerized task revisions, hidden tests, reproducible environments, task-level evidence, contamination and quality review |
| Eval libraries and red teaming | DeepEval 4.1.0, promptfoo 0.121.19, Ragas, Garak | Typed metrics, regression gates, adversarial cases, deterministic and model-based evaluators |
| Trace and eval platforms | Phoenix 18.0.0, Langfuse 3.213.0, MLflow 3.14.0, Weave 0.53.1, Opik 2.1.27 | First-class assessments, datasets and experiments, trace-linked evaluation, annotation workflows |
| Instrumentation | OpenTelemetry Python 1.43.0, OpenInference semantic conventions 0.1.30, OpenLLMetry 0.62.1, OpenLIT 1.24.0 | Vendor-neutral export, low-cardinality attributes, content capture off by default |
| Operations | OpenTelemetry Collector Contrib 0.156.0, Grafana Alloy 1.17.1, Tempo 3.0.2 | Redaction, batching, durable queues, tail sampling, separate authentication gateway |

Primary project sources:

- [Harbor releases and task model](https://github.com/harbor-framework/harbor/releases/tag/v0.18.0)
- [Inspect AI releases](https://github.com/UKGovernmentBEIS/inspect_ai/releases/tag/0.3.246)
- [SWE-bench releases](https://github.com/SWE-bench/SWE-bench/releases/tag/v4.1.0)
- [DeepEval releases](https://github.com/confident-ai/deepeval/releases/tag/v4.1.0)
- [promptfoo releases](https://github.com/promptfoo/promptfoo/releases)
- [Phoenix 18.0.0](https://github.com/Arize-ai/phoenix/releases/tag/arize-phoenix-v18.0.0)
- [Langfuse 3.213.0](https://github.com/langfuse/langfuse/releases/tag/v3.213.0)
- [MLflow 3.14.0](https://github.com/mlflow/mlflow/releases/tag/v3.14.0)
- [OpenLLMetry 0.62.1](https://github.com/traceloop/openllmetry/releases/tag/0.62.1)
- [OpenLIT 1.24.0](https://github.com/openlit/openlit/releases/tag/openlit-1.24.0)
- [OpenTelemetry semantic conventions 1.43.0](https://github.com/open-telemetry/semantic-conventions/releases/tag/v1.43.0)
- [OpenTelemetry Collector Contrib 0.156.0](https://github.com/open-telemetry/opentelemetry-collector-contrib/releases/tag/v0.156.0)

This is a representative review of the strongest active projects and standards,
not a claim that every agent-evaluation repository was inspected.

## Evidence that changed the design

Benchmark quality is a production control, not a one-time curation task.
OpenAI's July 8, 2026 audit of the 731-task public SWE-Bench Pro split found 200
tasks flagged by an agent pipeline and 249 by human review. OpenAI estimated
that roughly 30 percent had material problems. Its February 23, 2026 audit of
138 difficult SWE-bench Verified tasks found material issues in at least 59.4
percent of that audited subset and reported contamination evidence.

- [Separating signal from noise in coding evaluations](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)
- [Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)

The project therefore treats task identity, task QA, oracle behavior, dataset
revision, evaluator identity, and cohort compatibility as part of the result.
A benchmark score without this evidence is not promotion-grade.

Hosted products are not stable architectural foundations. OpenAI announced on
June 3, 2026 that the Agent Builder and Evals products will be unavailable on
the platform after November 30, 2026. Domain records and evidence in this
project therefore stay portable and backend-neutral.

- [AgentKit product update](https://openai.com/index/introducing-agentkit/)

OpenTelemetry's dedicated GenAI semantic-conventions repository had no release
tags at the cutoff. The reviewed revision was
[`93a59e48a9b4ea162a4d76edac4ace2d415a759e`](https://github.com/open-telemetry/semantic-conventions-genai/tree/93a59e48a9b4ea162a4d76edac4ace2d415a759e).
Those conventions are Development status. This project uses official GenAI
names only where the semantics match, isolates them behind a projection layer,
and keeps its canonical domain schema independent of that draft.

## Canonical records

The minimum durable model is:

```text
Experiment
  DatasetRevision
    DatasetItem
  Run
    RunAttempt
    PolicyDecision
    Artifact
    Assessment
      EvaluatorIdentity
```

An assessment binds a typed value to the run and includes source kind, status,
direction, range or threshold, evaluator version, optional model and rubric
digests, and optional dataset revision and item identity. Deterministic tests,
scanners, policies, humans, and model judges use the same envelope without
pretending they have the same semantics.

Rationale and source content are not assessment telemetry. Sensitive
explanations remain separately governed artifacts with stricter access and
retention.

## Trace boundary

The target operational hierarchy is:

```text
agent_eval.run
  governance.admission
  environment.prepare
  agent.execute
  workspace.snapshot
  evaluation.tests
  evaluation.scanner
  evaluation.judge
  outcome.decide
  persistence.save
  attestation.create
  cleanup
```

Low-cardinality attributes may include task class, agent adapter, model family,
phase, outcome, error category, evaluator name, and assessment dimension.
Prompts, source, diffs, transcripts, file paths, commands, URLs, raw tenant IDs,
run IDs, and trace IDs must never be metric labels. Raw content export is off by
default.

The `gen_ai.evaluation.result` event is reserved for an evaluation of model
output, such as a model judge. Tests, scanners, policy checks, and adversarial
challenges use `agent_eval.assessment.result`.

## Scanner promotion boundary

Ruff and Semgrep execute from a bundled Python 3.12 `uv` project with exact
top-level versions, a complete transitive lock, and a first-party Semgrep
ruleset. A packaged Gitleaks configuration forces its embedded defaults rather
than accepting a target-controlled `.gitleaks.toml`. Operators explicitly run
`agent-eval scanners prepare` to perform the bounded locked sync and, when
Trivy is installed, download its vulnerability database. `agent-eval scanners
identity` inspects the resulting materials without downloading anything.
Evaluation invokes `uv` with `--frozen`, `--offline`, and `--no-sync`, disables
Semgrep metrics and version checks, and tells Trivy to skip database updates.
The hash-bound invocation policy disables Ruff, Semgrep, and Gitleaks inline
suppressions, caller and target ignore files, and default target-size cutoffs.
Semgrep rejects incomplete reports. Gitleaks scans a bounded evaluator-owned
mirror in which source-root `.git` and `.gitleaksignore` paths are ordinary
scan data. Trivy receives evaluator-owned empty config and ignore policies.

The canonical scanner identity binds the complete runtime bundle, project,
lock, rulesets, a full-tree digest of the prepared Python environment, exact
executable SHA-256 values for `uv`, Python, Ruff, Semgrep, Gitleaks, and Trivy,
plus bounded Trivy metadata and an exact digest of its database file names and
bytes. Promotion readiness fails closed when a required scanner, version,
environment digest, executable digest, database digest, or completed scan is
missing. Governed policies allowlist the preflight identity, and completed runs
recompute it and reject a mismatch.

This freezes the admitted local scanner inputs. It does not supply comprehensive
language-specific analysis. The bundled Ruff and Semgrep profile is
Python-focused, while Gitleaks and Trivy provide cross-language secret and
dependency evidence. Additional language profiles require their own exact
runtime, rules, and promotion identity.

## Observed usage thresholds

Current requests use `agent-eval.request/v2` and name
`max_observed_total_tokens` and `max_observed_cost_usd` explicitly. The strict
loader accepts historical v1 requests with `max_total_tokens` and
`max_cost_usd`, then normalizes them to v2. Admission takes the lowest threshold
from policy, request, and the exact registered model. Outcome evaluation also
applies any stricter task acceptance threshold.

These controls use provider evidence after a trial. Missing or excessive token
or cost evidence rejects the run, and the CLI stops before starting a subsequent
governed trial. They do not interrupt an in-flight generation, reserve provider
spend, aggregate judge spend, or provide an atomic ledger across processes. A
multi-tenant service requires provider-side controls and transactional budget
reservation and accounting.

## Trust boundaries

Governed execution now requires an isolated black-box task contract. The
produced workspace goes only to a submission pod. Hidden tests and their result
volume go only to a separate evaluator pod, which starts with an empty
`/workspace`. The evaluator reaches the submission through its Pod IP on one
declared TCP port. Exact directional NetworkPolicies allow that flow and no
other evaluator or submission egress. Hidden tests receive only
`AGENT_EVAL_SUBMISSION_URL` and do not import or execute submitted code in the
evaluator process. Both pod image manifest digests become durable correctness
and governance evidence.

This implements the protected evaluator and result-writer process boundary. It
does not make the local cluster a complete hostile-code boundary. The pods run
on the same k3s worker and share its kernel unless an operator installs and
selects a reviewed RuntimeClass such as gVisor or Kata Containers. A kernel,
container-runtime, or network-plugin escape remains outside the guarantee.
Ordinary compatibility runs may still use cooperative evaluation, but governed
runs reject it before side effects.

Governed image construction is also outside the evaluation trust domain. The
task registry must preapprove one exact single-platform manifest for each
allowed Linux platform, including its content-derived reference, builder ID,
build type, source revision, and provenance SHA-256 assertion. Evaluation never
builds an admitted image. It verifies the local manifest, imports that exact
reference into every k3d node, uses `imagePullPolicy: Never`, and rechecks both
isolated grading pods against the approved digest. Node verification resolves
and hashes the exact containerd reference target. For an imported OCI index, it
selects exactly one real Linux child whose digest equals the expected platform
manifest, hashes that selected child, and ignores non-platform attestations.
Its config digest must equal both the CRI image config and the running pod's
image ID.

The remaining policy and registry boundary is explicit: the local policy,
builder fields, and provenance digest are unsigned assertions. The harness does
not authenticate a registry, verify a signature or transparency-log entry, or
establish trusted time. A production promotion service still requires an
isolated builder, signed policy and provenance, authenticated digest promotion,
and immutable registry retention.

Other promotion-grade production requirements remain:

1. Hardened workers using gVisor, Kata Containers, or disposable virtual
   machines.
2. Authenticated tenants, quotas, per-tenant policy, and separate production
   data-plane credentials.
3. PostgreSQL transactions plus encrypted object storage for content-bearing
   artifacts.
4. A durable outbox for telemetry and artifact delivery.
5. Signed attestations, trusted time, and a transparency log.

The local SQLite backend remains appropriate for a single-user workstation. It
is not the production multi-tenant system of record.

## Delivery decision

This branch implements the highest-risk foundation first:

- Exact task-tree and execution-recipe allowlisting in governance.
- Governed isolated black-box grading with separate submission and evaluator
  pods, directional network access, and evaluator-owned results.
- Preapproved per-platform task images with exact manifest and build assertions;
  governed evaluation never turns its own runtime build into an approval.
- A frozen Python 3.12 scanner runtime with exact project, transitive lock,
  first-party Semgrep rules, and offline evaluation execution.
- Scanner assurance over the runtime inputs and prepared environment, exact
  `uv`, Python, Ruff, Semgrep, Gitleaks, and Trivy executable hashes, and an
  exact digest of Trivy database file names and bytes.
- Promotion-ready scanner preflight allowlisting and a completed-scan identity
  recheck that fails closed on any change.
- Post-run observed token and cost thresholds that reject missing or excessive
  evidence and stop the CLI before a subsequent governed trial.
- Static-by-default corpus validation and bounded, sanitized opt-in execution.
- Safe package and state paths, strict identifiers, private atomic files, and
  versioned local database migrations.
- Exact credential redaction for durable agent output plus a bounded,
  no-follow workspace containment gate before evaluation and attestation.
- Ambiguous Kubernetes Secret creation rollback with exact resource identity,
  independent absence checks, and credential-free operator remediation.
- Versioned task and dataset metadata.
- Normalized assessments with privacy-safe optional OpenTelemetry export.
- Python entry-point adapters without built-in adapter shadowing.
- Wheel-install verification, security policy, contribution requirements, and
  an explicit Apache-2.0 license.

The checked-in governance policy is a schema example, not a ready approval.
Its zero scanner identity and placeholder image digest, reference, builder,
source revision, and provenance values must be replaced by promotion evidence.
Its task-tree and execution-recipe digests must also be regenerated for the
exact task revision and recipe. Copying those placeholders into a governed run
does not establish trust.

The remaining production work is intentionally explicit:

1. Hardened worker deployment and verification for the implemented protected
   evaluator boundary.
2. Signed policy, provenance, registry verification, trusted time, and an
   isolated image promotion service.
3. Authenticated control plane, tenant isolation, worker queue, quotas, and an
   atomic budget reservation and accounting ledger.
4. PostgreSQL repository, encrypted object storage, retention enforcement, and
   durable outbox.
5. Signed attestations and a transparency log for completed evidence bundles.
6. Language-specific lint and static-analysis profiles beyond the bundled
   Python-focused Ruff and Semgrep baseline.
7. Larger reviewed benchmark suites with continuous task health checks and
   contamination controls.
8. Collector deployment with authenticated ingestion, allowlist redaction,
   cardinality limits, span metrics, and tail sampling.
9. DLP controls for deliberately transformed credentials and sensitive source
   content beyond the exact credential containment implemented here.
10. Isolation for any credential broker that is not trusted operator code;
    portable process-group cleanup cannot contain a child that deliberately
    creates an independent session.

Until those controls exist and are verified in the target environment, the
project should be described as an enterprise-ready foundation, not a complete
multi-tenant production service.
