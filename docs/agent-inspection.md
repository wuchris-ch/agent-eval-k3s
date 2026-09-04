# Optional source and execution inspection

The general evaluator can assess internals as well as final answers. Add an
inspection profile to `blackbox run`, `replay`, or `replay-db`, or use `blackbox
inspect` on a saved report. The agent can use any framework or language.

The input/output suite, digest, scores, and `passed` field retain their original
meaning. Inspection adds separately hashed source and trace evidence, separate
results, and an `overall_passed` field. Internal checks are advisory by default.
`--require-inspection` makes every requested check mandatory for the overall
gate and CLI exit code. A correct answer can therefore pass the output checks
while failing a required execution check.

| Evidence | What is implemented | What the operator supplies |
|---|---|---|
| Input/output | CLI, HTTP, JSONL, and database evaluation | Frozen cases, agent interface or final observations |
| Source | Automatic selection and snapshotting of repository files, literal checks, optional DeepEval checks | Repository root, file patterns, inspection criteria |
| Execution | Tool presence, exclusions, sequence, call budget, recorded errors, optional DeepEval trace checks | A complete, correctly bound trace export |

Source access does not require execution tracing. Trace access does not require
source access. Neither is required for the shared output score. This extension
does not automatically instrument arbitrary agents, infer their entire repository
architecture, or generate tests from source.

## Inspect source during a live run

This example needs no credentials or optional dependencies:

```sh
uv run agent-eval blackbox run \
  --suite examples/blackbox/faq.yaml --agent faq-smoke \
  --command "python3 $PWD/examples/blackbox/smoke_target.py" \
  --inspection examples/blackbox/source-inspection.yaml \
  --source-root "$PWD/examples/blackbox" --require-inspection
```

`source-inspection.yaml` selects `smoke_target.py` and checks for its answer
handler. Source is read before the run and checked again afterward. A changed
selection makes the source evidence unavailable. Inspection never imports,
executes, or modifies selected agent code.

## Run the instrumented example

The example records actual OpenTelemetry spans for a deterministic fixture with
`search` and `validate` functions. It exercises collection, correlation, replay,
and separate output and inspection gates. It is not an AI quality benchmark.

```sh
uv sync --extra observability
inspection_fixture_dir=$(uv run python examples/blackbox/instrumented_fixture.py)

uv run agent-eval blackbox replay \
  --suite examples/blackbox/faq.yaml --agent instrumented-fixture \
  --observations "$inspection_fixture_dir/observations.jsonl" \
  --inspection examples/blackbox/inspection.yaml \
  --source-root "$PWD/examples/blackbox" \
  --traces "$inspection_fixture_dir/traces.jsonl" --require-inspection
```

Both output cases, the source check, and all trace checks pass. To demonstrate
the distinction, generate a second recording:

```sh
inspection_fixture_dir=$(uv run python examples/blackbox/instrumented_fixture.py --wrong-order)
```

Repeat the replay command. The output score is still `1.0`, but the trace checks
reject validation before search. The overall gate fails with exit code `2`.
Each fixture recording and evaluation uses a new private directory.

## Define an inspection profile

Profiles are separate YAML or JSON files. Changing internal checks does not
change the shared suite digest.

```yaml
schema_version: "1.0"
source:
  include: ["src/**/*.py", "prompts/*.md"]
  exclude: ["**/fixtures/*"]
checks:
  - name: validation-handler-present
    kind: source_contains
    path: src/agent.py
    text: "def validate("
  - name: uses-search
    kind: tools_required
    tools: [search]
  - name: validates-after-search
    kind: tool_sequence
    tools: [search, validate]
  - name: bounded-tool-calls
    kind: tool_budget
    max_calls: 5
```

Use a source-only profile without trace checks, or a trace-only profile without
the `source` section. Paths and patterns are relative to `--source-root`.
Inspection rejects absolute paths, traversal, and selected symlinks. It skips
dependency and credential directories, `.env` files, and common credential/key
filenames. Limits are 100 selected UTF-8 files, 512 KiB of source content, and
20,000 enumerated entries. Only deliberately selected source belongs in a
profile; filename exclusions are not a general secret detector.

| Check | Semantics |
|---|---|
| `source_contains` | Case-sensitive literal `text` occurs in the selected `path`. |
| `source_not_contains` | Literal `text` is absent from the selected `path`. A missing file is unavailable. |
| `source_geval` | DeepEval assesses `criteria` using selected source, case context, input, expected output, and actual output. |
| `tools_required` | Every listed tool has at least one recorded call. |
| `tools_forbidden` | None of the listed tools has a recorded call. |
| `tool_sequence` | A call to each tool in order finishes before the next starts. Extra calls are allowed; a span cannot be reused. |
| `tool_budget` | Total recorded tool calls are at most `max_calls`. |
| `trace_no_errors` | No normalized span has error status. |
| `trace_geval` | DeepEval assesses `criteria` using the normalized execution trace and the case's input/output evidence. |

Literal source checks run once per source snapshot. Source GEval and all trace
checks run per case/trial. Failed calls count toward presence and budget checks.
Use `trace_no_errors` as well when errors should fail inspection. An unset span
status means no explicit error was recorded, not proof of successful execution.
Literal source checks establish text presence, not semantic correctness.

For semantic checks, use `examples/blackbox/inspection-geval.yaml` with `--judge`
and the [judge configuration](blackbox-evaluation.md#metrics-and-outcomes).
The judge receives selected source or trace content, including supplied tool
arguments/results. Output metrics still receive only the original case context.
GEval failures remain unavailable inspection results, independently of output
quality. A source snapshot describes the supplied files; it does not establish
that a remote deployment executed that exact version.

## Bind traces to observations

`--traces` accepts normalized JSONL, an array of normalized records, or an OTLP
JSON request containing `resourceSpans`. In a live run the file is read after
agent execution, so the observer must finish exporting before inspection starts.
Replay is useful when the trusted harness records observations and traces
together, as the example does. Target input remains unchanged; the evaluator
does not inject case IDs, goldens, or tracing fields into an agent's request.

Each normalized `TraceRecord` contains:

| Field | Requirement |
|---|---|
| `case_id`, `trial` | Exact evaluated case and trial number. |
| `input_sha256` | `digest(case.input)` from `agent_eval.blackbox.models`. |
| `output_sha256` | `digest(observation.actual_output)` using the same canonical encoding. |
| `trace_id`, `root_span_id` | Lowercase nonzero OTel IDs, 32 and 16 hex characters. |
| `complete` | Explicit boolean asserted by the trusted collector or harness. |
| `spans` | Root and all descendants relevant to this evaluation. |

Each span has `span_id`, `parent_span_id` (null on the root), `name`, `kind`
(`agent`, `tool`, `model`, `internal`), `status` (`ok`, `error`, `unset`), and
integer `start_ns` and `end_ns`. Tool spans require `tool_name`. Optional
`arguments` and `output` can be any finite JSON value. All spans must be ended
and form a connected, acyclic graph with unique IDs.

For OTLP JSON, place these binding attributes on exactly one evaluation root
per trace:

```text
agent_eval.case.id          string
agent_eval.trial.number     integer
agent_eval.input.sha256     string
agent_eval.output.sha256    string
agent_eval.trace.complete   boolean
```

Tool spans use `gen_ai.tool.name`; arguments and results use
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`. Structured OTLP
arrays/objects and string-serialized values are preserved. `execute_tool` spans
without a tool name are invalid. Error status or `error.type` normalizes to an
error. Dropped attributes make the trace incomplete. Resource metadata, arbitrary
attributes, span events, and links are not included in the normalized projection.
OTLP IDs use hex and status codes use integers, per the
[OTLP JSON encoding](https://opentelemetry.io/docs/specs/otlp/#json-protobuf-encoding).
Tool fields follow the [OpenTelemetry GenAI span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md),
checked September 4, 2026.

Unbound OTLP traces are ignored. A bound trace is checked for broken parents,
cycles, duplicates, and disconnected spans before selecting the evaluation
root's subtree. Connected surrounding server spans can be omitted from that
subtree. Each evaluation allows one record per case/trial, up to 2,000 spans and
512 KiB per normalized record, 20,000 spans total, and a 16 MiB import file.

Missing, partial, malformed, or mismatched traces are `unavailable`, never a
passing negative check. Input/output digests must match, and observation OTel
IDs must agree when supplied. Digests check alignment, not authenticity or
freshness. The trusted observer must isolate the correct run and ensure no
sampling, dropped spans, or omitted tool calls before asserting completeness.
An agent's own assertion that it used the right tools is insufficient evidence.

## Inspect an existing report

```sh
uv run agent-eval blackbox inspect \
  --report /absolute/path/saved-run/report.json \
  --inspection /absolute/path/inspection.yaml \
  --source-root /absolute/path/agent-repository \
  --traces /absolute/path/traces.jsonl --require-inspection
```

The command reads the neighboring saved `suite.json`, preserves original output
results and suite digest, and writes a new report with `parent_run_id`. It does
not invoke the agent or regrade output metrics. Source captured here reflects
inspection time, not necessarily the original execution time. Use a pinned
checkout when investigating an older release.

Inspected reports use schema `1.1`. Their `inspection` section includes separate
source/trace summaries, per-check reasons/errors, profile and evidence hashes,
and the required/advisory setting. Unavailable checks have null scores. The
original output `passed` field and `average_score` remain suitable for comparing
agents on the same shared suite and judge settings. Compare internal results
only where visibility and inspection profiles are comparable.

Private run artifacts add `inspection-profile.json`, `source.json` when source
is available, and normalized `traces.jsonl` when traces are available. Source
file hashes and a canonical manifest hash detect snapshot changes. The local
SQLite `inspection_runs` table indexes these results separately from
`blackbox_runs`. New inspection runs can be identified by `parent_run_id` to
avoid counting the original outputs twice.

Optional evaluator telemetry exports only hashes, check kinds, scores, statuses,
and error codes. Raw source, tool names, arguments, results, and judge reasons
remain in private artifacts, except for the explicit judge request when enabled.
