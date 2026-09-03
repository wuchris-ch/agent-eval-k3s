# Contributing

## Local setup

On macOS, install the external tools and synchronize the locked environment:

```sh
brew install uv k3d gitleaks trivy
uv sync --frozen --all-extras
```

Docker Desktop and `kubectl` are also required for end-to-end task runs. Most
unit tests do not need a cluster.

## Required checks

Run these before opening a pull request:

```sh
uv run ruff check .
uv run pytest -q
uv lock --check --project src/agent_eval/scanner_runtime
uv build --no-sources
```

Ruff and Semgrep run from the dedicated Python 3.12 project in
`src/agent_eval/scanner_runtime`. Keep its top-level versions exact and commit
both `pyproject.toml` and the regenerated `uv.lock` when changing either
scanner:

```sh
uv lock --project src/agent_eval/scanner_runtime --python 3.12
```

Do not replace this runtime with `uvx` or an unlocked install. The default
scanner rules must remain first-party, license-safe, bounded, and packaged in
`semgrep.yml`; do not replace them with a mutable registry configuration.

Only execute the checked-in reviewer corpus after reviewing and trusting its
reproducer commands:

```sh
uv run agent-eval corpus validate \
  benchmarks/reviewer-corpus/v1/corpus.yaml \
  --allow-local-execution
```

## Change requirements

- Keep schemas strict, versioned, and backward compatible within a schema
  version.
- Preserve missing evidence as missing. Do not coerce missing measurements to
  zero or success.
- Keep audit records free of prompts, source code, transcripts, credentials,
  command output, and evaluator rationale.
- Add tests for denial paths, malformed input, partial failures, and replay.
- Record exact model, image, task, evaluator, ruleset, and dataset revisions
  whenever a claim depends on them.
- Never execute corpus or change-controlled commands without explicit opt-in.
- Avoid adding a backend-specific observability or storage dependency to the
  domain model.

## Pull requests

Describe the trust boundary affected by the change, the evidence added or
removed, migration compatibility, and the checks run. Security-sensitive
changes should include a concise abuse-case analysis.
