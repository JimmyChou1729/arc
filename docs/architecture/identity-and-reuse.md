# Identity and Reuse

This document is the canonical identity policy for ARC packages. It prevents an
operationally unrelated change from invalidating reusable work, while ensuring
that semantically different work never aliases.

## Separate keys

ARC uses distinct typed concepts:

- **Semantic task key**: what result the caller requested. It includes stable
  business input and explicit semantic requirements.
- **Execution fingerprint**: how an unfinished effect is being executed. It
  includes the resolved provider/model, effective invocation recipe, adapter
  compatibility version, and native-session compatibility data.
- **Operational policy**: controls for this invocation, such as concurrency,
  deadline, retry or recovery limits. It is not an identity key.
- **Effect-request digest**: the exact request protected by an external-effect
  journal barrier.
- **Artifact digest**: the bytes of one immutable artifact.
- **Resume-input digest**: one response to one opaque resume key.

These values are not interchangeable. APIs and durable schemas must use their
qualified names and typed value objects; no generic `fingerprint`, `identity`,
or caller-supplied digest may stand in for another category.

## Run identity

The durable run scope is:

```text
repository root + handler contract + run ID
```

`RunRepository` derives the semantic key from the handler and canonical
`RunSpec.semantic_input`. The public `RunSpec` contains only `run_id`,
`handler`, and `semantic_input`; callers cannot submit a claimed digest.

The same scope and semantic key replays the existing lineage. The same scope
with a different semantic key fails before a handler, provider, or external
effect runs.

Semantic input must not contain timestamps, physical paths, run-root location,
package versions, generated IDs, credentials, resolved auto-provider choices,
timeouts, concurrency, retry limits, or execution-slice settings. Those values
do not change what result was requested.

## LLM identity

An LLM semantic key includes the task ID, prompt, output contract, requested
model/tier/capability requirements, accepted session-prefix digest, and
upstream content digests. An explicitly pinned provider or model is a semantic
requirement. A provider/model selected from `auto` belongs only to the
execution fingerprint.

Changing operational policy must not invalidate an accepted result. Changing
an execution fingerprint prevents unsafe continuation of an unfinished native
session but does not erase an already accepted semantic result.

## Proposer-reviewer identity

Worker task identity is derived from stable role, loop/round/worker identifiers,
business context, prompt/output contracts, and upstream content digests. It
must not include the outer physical run ID, artifact paths, observed provider
usage, or batch/proposer concurrency.

## Artifact reuse

Artifacts are addressed by a hierarchical logical artifact ID and verified by
their own `ArtifactDigest`. Logical paths are locators, not content identity.
Cross-run adoption is allowed only within one repository root:

1. `arc-jobs` resolves and verifies the source bytes and expected digest.
2. The owning package revalidates its business output contract.
3. `arc-jobs` re-verifies the digest, publishes a target immutable artifact,
   and records immutable source provenance.

The same target ID, bytes, media type, and provenance replays. Conflicting
bytes or provenance never overwrite an existing artifact.

## Schema evolution

All v1 durable and wire decoders reject unknown fields. Adding even an optional
field or enum value therefore requires a new schema version. Migration code
must be explicit; a package version bump never silently changes identity.
