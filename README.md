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
- Starts evaluation in a fresh pod so filesystem changes from the agent phase
  cannot carry over. Produced code is not isolated from the in-pod grader.
- Supports both full agent runs and eval-only scoring for workspaces produced
  elsewhere.
- Tracks correctness, pass@k, coverage, wall time, token usage, tool calls,
  diff size, scanner findings, and judge scores in SQLite-backed run records.
- Provides pluggable adapters for Claude Code and OpenAI Codex CLI.
- Scores any review agent's JSON output against gold-labeled findings with
  precision, recall, F1, blocker/major recall, false-positive rate, clean-PR
  accuracy, and Wilson 95% intervals.
- Emits SARIF 2.1.0 with GitHub's supported location fingerprint and stable
  severity-independent rule identity, alongside human-readable and native JSON
  reports. GitLab continuity follows its third-party SARIF path/line behavior.
- Runs arbitrary-code pods with no service-account token, `RuntimeDefault`
  seccomp, no privilege escalation, dropped Linux capabilities, and
  task-configurable resource bounds.

## Resume-ready positioning (July 2026)

Built an independent assurance layer for coding agents and AI pull-request
reviewers: Kubernetes-isolated agent trials, fresh-pod phase isolation for
hidden-test evaluation, evidence-verified review findings, deterministic
gold-label scoring with uncertainty intervals and false-positive gates, and
SARIF output for enterprise CI.

That is intentionally different from building another PR comment bot. By 2026,
repository context, custom instructions, agentic fixes, and multi-agent reviews
are standard vendor features. The harder enterprise question is whether a
reviewer is accurate, reproducible, cost-effective, and safe enough to gate a
merge. This project supplies the independent measurement and enforcement layer.

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
   Agent-phase filesystem mutations cannot persist into this pod. Produced code
   can still read `/tests` and forge or modify artifacts under `/results`, so
   this is phase isolation rather than a separate grader trust boundary.
4. **Scan phase** — host-side ruff, semgrep, gitleaks, and trivy over the
   produced workspace (each degrades gracefully if not installed).
5. **Judge phase** — the Claude API scores the diff against the task prompt on
   the task's rubric (spec adherence, maintainability, test quality).
6. **Persist** — everything lands in `runs/<run-id>/` plus a SQLite row.

## Concepts: the stack from the ground up

This section teaches the ideas the harness is built on, starting from zero.
Each layer only assumes the one before it. If you already know Kubernetes,
skip to layer 4, where it becomes specific to this repo.

### Layer 0: the problem being solved

A coding agent is a program that writes and *runs* arbitrary code. To evaluate
one fairly you need three things: **isolation** (its mistakes or `rm -rf`
can't touch your machine or the previous trial), **reproducibility** (trial 7
starts from the exact same toolchain and files as trial 1), and
**trustworthy grading** (the agent must not be able to see the answer key or
sabotage the thing that grades it). Everything below exists to buy those three
properties cheaply.

### Layer 1: containers and images

A **container** is a normal Linux process that the kernel has been told to
lie to: it sees its own filesystem, its own process list, its own network,
even though it shares the machine's kernel with everything else. It is *not*
a virtual machine, there is no second OS booting, which is why containers
start in milliseconds.

An **image** is the frozen filesystem a container starts from: a stack of
tarballs plus metadata (default command, working dir, env). Images are built
from a `Dockerfile`, a script of steps like "start from python:3.12, install
pytest, copy these files in". Build once, run identical copies forever, and
that is the reproducibility property: in this repo, each task's
`environment/Dockerfile` bakes the language toolchain, the agent CLIs, and
the starter workspace into one image, so every trial begins from a
bit-identical world.

On macOS there is no Linux kernel, so Docker (or colima) runs one small
hidden Linux VM, and all containers live inside it. That detail matters once,
in layer 3.

### Layer 2: Kubernetes in one idea

With plain Docker you *imperatively* run containers: `docker run this`,
`docker stop that`, and you babysit them. **Kubernetes (k8s)** flips this to
a *declarative* model: you submit a description of what should exist ("a pod
named `eval-a1b2c3d4` running image X, killed automatically after 3600s") to
an **API server**, and controllers work to make reality match the
description. You never start processes directly; you edit desired state and
the cluster converges to it.

The objects this harness touches, and this is genuinely all of Kubernetes you
need for this repo:

- **Pod** — the unit of running stuff: one or more containers scheduled onto
  a node, sharing a network identity. Here every pod is a single container.
- **Node** — a machine (real or fake, see layer 3) that runs pods.
- **Namespace** — a folder for objects. Everything here lives in the
  `agent-eval` namespace so it can't collide with anything else and can be
  deleted wholesale.
- **Secret** — a stored key/value blob (here: `ANTHROPIC_API_KEY`) that pods
  can opt into as environment variables, so credentials are injected at
  runtime instead of being baked into images.
- **kubectl** — the CLI that talks to the API server. `kubectl apply -f -`
  submits a JSON/YAML object; `kubectl exec` runs a command inside a live
  pod; `kubectl wait` blocks until a condition (like `Ready`) is true.

Two pod-spec fields do quiet heavy lifting in this repo:
`restartPolicy: Never` (a crashed sandbox should stay dead, not resurrect and
rerun the agent) and `activeDeadlineSeconds` (the cluster itself kills the pod
after N seconds, a dead-man switch that holds even if the harness process on
the host dies mid-run).

### Layer 3: k3s and k3d, or "a cluster on your laptop"

Real Kubernetes is heavy: multiple binaries, etcd, cloud integrations.
**k3s** is a CNCF-certified distribution that strips all that into a single
~70MB binary with an embedded database, built for edge devices and CI. It
speaks the exact same API, so `kubectl` and pod specs don't know the
difference.

**k3d** goes one step further: it runs k3s *inside Docker containers*. Each
"node" of your cluster is just a Docker container running the k3s binary. So
the full stack on this Mac is:

```
macOS
└── Docker/colima's Linux VM
    ├── container: k3d-agent-eval-server-0   (k3s control plane + node)
    ├── container: k3d-agent-eval-agent-0    (k3s worker node)
    └── the pods you create run as processes inside those node containers
```

Why bother with the middle layers instead of plain `docker run`? Because the
declarative API gives you `wait --for=condition=Ready`, `activeDeadlineSeconds`,
labels, and namespaced cleanup for free, and because the same harness code
would work unchanged against a real remote cluster if you ever wanted to run
50 trials in parallel on rented hardware. k3d is the cheapest thing that
speaks that API.

One consequence of nodes-in-Docker: the cluster has its **own image store**
(containerd inside the node containers), separate from your host Docker
daemon. An image you `docker build` on the host is invisible to the cluster
until you copy it across with `k3d image import`. That is exactly what
`build_and_import_image()` in `src/agent_eval/cluster.py` does, and why pods
use `imagePullPolicy: IfNotPresent`: never pull from a registry, use the
imported copy.

### Layer 4: how the harness actually drives the cluster

All cluster interaction is `kubectl` subprocess calls, no Kubernetes client
library (`src/agent_eval/kube.py` is ~100 lines). Three tricks make that
enough:

1. **The sleeping sandbox.** Every pod is created with the command
   `sh -c "sleep infinity"`. The pod does nothing by itself; it is an idling
   container the harness reaches into with `kubectl exec` to run each step
   (`mkdir`, the agent CLI, the test command). This turns a pod into a
   disposable remote shell with a known filesystem, which is a much simpler
   model than encoding the whole pipeline into the pod's command.
2. **tar pipes instead of `kubectl cp`.** To copy a directory in, the harness
   tars it on the host and pipes the stream into `kubectl exec -i <pod> --
   tar -xf -`; to copy out, the reverse. `kubectl cp` is notoriously flaky
   with directories and symlinks; a tar stream over the exec channel is the
   standard reliable workaround.
3. **UUID-named, label-tagged pods.** Pods are named `agent-<hex>` /
   `eval-<hex>` and labeled `app=agent-eval`, so concurrent trials can't
   collide and stragglers are easy to list and delete.

`cluster.py` handles lifecycle: `k3d cluster create agent-eval --agents 1`
on first use, the `agent-eval` namespace, and syncing the `agent-api-keys`
secret from your shell's `ANTHROPIC_API_KEY` (which is why the README says to
re-run `cluster up` after changing the key: the secret is a copy, not a
reference).

### Layer 5: the two-pod trust model

The core design decision of the harness is that each trial uses **two pods
with different privileges**, both from the same task image:

| | agent pod | eval pod |
|---|---|---|
| contains | starter workspace, agent CLI, prompt | agent's produced workspace + hidden tests at `/tests` |
| credentials | API key secret / codex auth | none |
| hidden tests | never present | present |
| lifetime | agent phase only | eval phase only |

The agent pod never contains the hidden tests, so the agent cannot read the
answer key or special-case it. And grading happens in a *fresh* pod, so
nothing the agent did (monkey-patching pytest, editing installed packages,
planting a `conftest.py` trap, poisoning caches) survives into the
environment that judges it. Only the agent's `/workspace` files are carried
across, as a tar snapshot, and diffed against the starter to measure the
change. This is the same reason SWE-bench-style evals re-run tests in clean
containers: the moment the graded environment is one the agent could write
to, pass/fail stops being evidence.

The remaining trust gap is documented in the credential note below: the agent
pod *does* hold real credentials and has network egress, because the agent
needs to call its own model API. Isolation here is about protecting the
*evaluation*, not about containing a malicious agent; containers share the
host kernel and are not a hard security boundary.

### Layer 6: a trial, end to end, in kubectl terms

Tying it together, `run_agent_trial()` in `src/agent_eval/runner.py` is this
sequence:

1. `docker build` + `k3d image import` (skipped if the image already exists).
2. Apply an **agent pod** spec (sleep-infinity, secret env, deadline =
   agent timeout + 900s grace); `kubectl wait --for=condition=Ready`.
3. tar-pipe the prompt in; `kubectl exec` the agent CLI headless, capturing
   stdout as a JSONL transcript (tokens, turns, cost come from parsing it).
4. tar-pipe `/workspace` out, delete the agent pod. From here the agent no
   longer exists; only its files do.
5. Apply an **eval pod**; tar-pipe in the produced workspace and the hidden
   tests; `kubectl exec` the task's test command; tar-pipe `/results`
   (junit XML, coverage) back out; delete the pod.
6. Host-side: diff vs. starter, scanners, LLM judge, persist to
   `runs/<run-id>/` and SQLite.

### Poking at it yourself

The cluster is ordinary Kubernetes, so standard commands work and are the
fastest way to build intuition:

```sh
k3d cluster list                          # does the cluster exist?
kubectl config get-contexts              # harness uses context k3d-agent-eval
kubectl -n agent-eval get pods           # watch agent/eval pods during a run
kubectl -n agent-eval exec -it <pod> -- sh   # shell into a live sandbox
kubectl -n agent-eval describe pod <pod> # events: why is it stuck Pending?
kubectl -n agent-eval get secret agent-api-keys -o yaml
k3d cluster delete agent-eval            # nuke everything; `run` recreates it
```

A useful habit while a trial runs: `watch kubectl -n agent-eval get pods` in
a second terminal. You'll see the `agent-…` pod appear, run for minutes, get
replaced by an `eval-…` pod for seconds, then everything vanish. That
rhythm *is* the pipeline.

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
results, and a verified-findings LLM review. The same directory always includes
`review.sarif`, an explicitly PR-diff-scoped export with GitHub's supported
location fingerprint and repo-relative locations suitable for code-scanning
upload. Exit code is 2 when risk is high or any blocking grader fails, so it
drops into CI as a check.

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

### Benchmark an AI reviewer

`benchmark-review` is vendor-neutral: export each reviewer run as either this
project's `review.json` or a simple `{"findings": [...]}` JSON file. The scorer
uses exact file/category matching and a gold line range, then computes a maximum
one-to-one match. No LLM judges whether the LLM was correct.

```yaml
# benchmark.yaml
cases:
  - id: auth-bypass
    description: Authorization check removed from the update path
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

Place outputs at `<reviews>/<case-id>.json`, then score and optionally turn the
metrics into a regression gate:

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

The item-level JSON records every matched, missed, and unmatched finding. The
aggregate includes precision/recall/F1, blocker+major recall, severity accuracy,
false positives per case and per KLoC, clean-case accuracy, and Wilson 95%
intervals. Missing files and absent or null findings payloads are visible,
score as zero findings, and cannot earn clean-case credit. The CLI fails closed
on incomplete outputs by default; use `--allow-missing` only for exploratory,
intentionally partial runs.

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

The pod profile removes ambient Kubernetes identity and common privilege-
escalation paths, but it is not a complete hostile-code boundary: task images
still use their declared user and writable filesystem, and model API access
requires network egress. The eval pod also does not protect hidden tests or
in-container result artifacts from the produced code being evaluated. Non-root
task images, protected out-of-process result collection, short-lived
credentials, and a domain-aware egress proxy are the next hardening steps.

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

## Enterprise roadmap

The next highest-value extensions are deliberately measurable:

1. Add a public, versioned corpus of faulty and clean PRs with executable hidden
   reproducers, repeated trials, paired comparisons, latency/cost curves, and
   cross-run stability.
2. Bind commit SHAs, policy hashes, image digests, tool versions, results, and
   artifact hashes into a locally verifiable in-toto/SLSA-style attestation;
   add optional Sigstore signing only after local verification is solid.
3. Add an OWASP Agentic Top 10 challenge pack for poisoned instructions, hidden-
   test discovery, grader tampering, credential exfiltration, tool misuse, and
   resource exhaustion.
4. Add default-deny network policy through a domain-aware egress proxy and move
   agent authentication from reusable host credentials to per-trial credentials.
5. Compare single-reviewer and specialist-panel modes at a fixed false-positive,
   latency, and token budget before claiming that multiple agents improve review.

Design references: [NIST AI 800-2 draft evaluation guidance](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.800-2.ipd.pdf),
[OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/),
[Kubernetes Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/),
[SLSA 1.2](https://slsa.dev/spec/v1.2/), and
[GitHub SARIF integration](https://docs.github.com/en/code-security/concepts/code-scanning/sarif-files).

## The whole project in plain English

Think of this project as three related quality-control machines:

```text
1. Coding-agent exam
   task -> agent writes code -> hidden tests and scanners -> scorecard

2. Pull-request safety check
   git diff -> tests, policies, scanners, and evidence-checked AI review -> report

3. Reviewer benchmark
   known bugs + reviewer findings -> precision, recall, false positives -> CI gate
```

The shared idea is simple: **do not trust a confident answer when you can ask
for evidence and measure it.**

### Machine 1: give a coding agent an exam

Imagine the task says:

> Add priorities to a todo API and support deleting todos.

The harness does this:

1. It creates a temporary Kubernetes pod containing the starter project.
2. Claude Code or Codex reads the prompt and edits the project inside that pod.
3. The harness saves the resulting workspace, transcript, time, token use, and
   cost information.
4. It deletes the agent pod and starts a fresh evaluation pod.
5. It copies in the produced code and hidden tests, then runs the test command.
6. It also runs security and quality scanners and records all results.

Why use a fresh second pod? Suppose the agent changed an installed test tool or
left a background process running. Those agent-phase changes disappear with the
first pod. Only the produced workspace moves forward.

The important limitation is equally simple: the produced program still runs in
the evaluation pod beside `/tests` and `/results`. It could inspect those files
or forge result files. The design isolates phases; it is not yet a hostile-code
proof grader.

### Machine 2: check a pull request before merge

Now imagine a PR changes this:

```python
if user.is_admin:
    delete_account(account_id)
```

to this:

```python
delete_account(account_id)
```

`agent-eval review` combines several kinds of evidence:

- **Scope policy:** was a protected path changed?
- **Commands:** do lint, build, or type checks still pass?
- **Tests:** does the new code pass, and do new tests actually fail on the old
  code?
- **Scanners:** did the change add a secret or security finding?
- **AI review:** can the model identify a real risk and quote the exact changed
  code that proves it?

The AI is not allowed to raise risk from a vague opinion. Its quote must exist
in the diff, and serious findings get a second adversarial verification pass.
The output is written as Markdown for people, JSON for automation, and SARIF for
code-scanning systems.

### Machine 3: test whether a reviewer is actually good

An AI reviewer saying “I found three bugs” is not enough. We need an answer key.

Suppose a small benchmark contains two PRs:

- `auth-bypass` has one known blocker at `src/auth.py:81-86`.
- `clean-refactor` intentionally has no bug.

If a reviewer finds the auth bug but also invents a bug in the clean PR, the
score is:

```text
true positives:  1   known bugs found
false positives: 1   invented findings
false negatives: 0   known bugs missed
precision:       50%  half of its findings were right
recall:         100%  it found every known bug
```

That is why this project reports both recall and false positives. A reviewer
that comments on everything can get high recall while being unusably noisy.
Clean-PR cases expose that behavior.

Matching is deliberately mechanical: file, category, and line range must match.
Another LLM does not get to decide whether the first LLM was correct. CI can then
enforce rules such as “critical recall must be at least 90%” and “no more than
0.5 false positives per PR.”

### What Kubernetes contributes

You only need four Kubernetes ideas to understand the runtime:

| Kubernetes term | Plain-English meaning | This project uses it for |
|---|---|---|
| image | a reusable room template | identical tools and starter files |
| pod | one temporary room made from the template | one agent or evaluation phase |
| secret | a sealed envelope of credentials | model API authentication |
| resource limits | a room's power, memory, and disk budget | bounded, comparable trials |

The pod profile also removes the Kubernetes service-account token, drops Linux
capabilities, disables privilege escalation, and applies the default seccomp
profile. These are useful guardrails, not a claim that containers are perfect
security boundaries.

### Where the main code lives

| File | Responsibility |
|---|---|
| `cli.py` | commands a person or CI invokes |
| `runner.py` | coding-agent and evaluation pipeline |
| `kube.py` | pod manifests and `kubectl` operations |
| `review.py` | PR evidence collection and risk calculation |
| `review_benchmark.py` | gold-label matching and reviewer metrics |
| `sarif.py` | code-scanning output with stable identities |
| `task.py` | task, timeout, rubric, and resource configuration |

### What makes this enterprise-relevant

Enterprise teams do not only ask, “Can the agent write code?” They ask:

- Can we reproduce the result?
- Can we explain why a merge was blocked?
- Can we compare models without ignoring cost or false positives?
- Can policy be enforced by code instead of prompt wording?
- Can existing CI and code-scanning systems consume the evidence?
- Are security limits and remaining trust gaps documented honestly?

This repository is the measurement and evidence layer for answering those
questions. It does not claim that one small local benchmark proves a model is
safe, or that a Kubernetes container can contain fully malicious code. The next
step is a larger public PR corpus and a protected out-of-process grader.

## Writing a task

```
tasks/<task-id>/
├── task.yaml               # prompt, timeouts, resources, test command, rubric
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
- Agent and eval pod resources are independently configurable. Omitting this
  block uses the documented defaults:

  ```yaml
  resources:
    agent:
      requests: {cpu: "100m", memory: "128Mi", ephemeral-storage: "256Mi"}
      limits: {cpu: "2", memory: "2Gi", ephemeral-storage: "4Gi"}
    eval:
      requests: {cpu: "100m", memory: "128Mi", ephemeral-storage: "256Mi"}
      limits: {cpu: "2", memory: "2Gi", ephemeral-storage: "4Gi"}
  ```

  Partial request or limit maps inherit these defaults. Quantities must be
  positive, and each request must not exceed its corresponding limit.
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
