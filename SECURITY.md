# Security policy

## Supported versions

Security fixes are made on the default branch. Until the project publishes
versioned releases, no older revision receives separate security support.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability that could put users,
credentials, evaluation data, or infrastructure at risk. Contact the maintainer
at `chriswu.ca@gmail.com` with:

- The affected revision or package version.
- Reproduction steps or a proof of concept.
- The impact and required preconditions.
- Any known workaround.

Do not include live credentials, proprietary source code, raw transcripts, or
personal data. Use synthetic evidence where possible. The maintainer will
coordinate a private acknowledgement, validation, fix, and disclosure timeline.

## Security model

The project treats model output, evaluated workspaces, repository instructions,
and benchmark corpora as untrusted data. Local task Dockerfiles, explicitly
enabled local commands, the host Docker daemon, cluster administrators, and the
machine running the harness are trusted boundaries.

Kubernetes containers are a resource and process isolation control. They are not
a sufficient boundary for deliberately malicious native code on a shared kernel.
For hostile workloads, use a hardened runtime such as gVisor, Kata Containers,
or a disposable virtual machine and keep the evaluator and result writer outside
the submitted-code trust domain.

Never use production credentials for adversarial trials. Prefer scoped,
short-lived credentials issued for a dedicated evaluation account.

The runner removes exact projected credentials and JSON-escaped forms from
durable agent output, stages and inspects returned workspaces before promotion,
and rejects a detected leak before evaluation or attestation. An agent that can
read a credential can deliberately transform it. Fragmented, encrypted, hashed,
or independently encoded secrets require a separate DLP control and are outside
this exact-containment guarantee.

`AGENT_EVAL_CREDENTIAL_COMMAND` is trusted operator configuration. Its process
group is terminated and its output is bounded, but a deliberately hostile
native broker can create a new session and escape portable process-group
cleanup. Isolate untrusted credential issuers behind a supervised service or a
hardened worker boundary.

## Dependency and deployment handling

The scanner runtime's current advisory disposition is documented in the
[PYSEC-2026-2132 reachability exception](https://github.com/wuchris-ch/agent-eval-k3s/blob/main/docs/security-exceptions/PYSEC-2026-2132.md),
including its fixed review deadline and removal criteria.

- Pin runtime images and CI actions by digest or commit.
- Review dependency lock changes and scanner database freshness before release.
- Apply backward-compatible database migrations before dependent code.
- Do not mark a production release complete until the application and database
  are both healthy.
