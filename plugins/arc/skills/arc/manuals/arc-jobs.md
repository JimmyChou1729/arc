# ARC Jobs Quick Start

`arc-jobs` inspects and controls durable runs created by another ARC package.
Use it when an owning workflow exposes a generic run root and run ID but does
not offer a more specific status, validation, or stop command. For a direct-root
owner such as `arc-llm`, retain the exact `--run-root` you supplied and the
`run.id` it returned. Project-based owners such as `arc-domain`,
`arc-translate`, and `arc-companion` already expose project-aware lifecycle
commands; use those instead of inferring their internal job roots. `arc-jobs`
does not create or resume package work.

## Run ARC Jobs

Examples below assume `arc-jobs` is on `PATH`. Check once with:

```bash
arc-jobs --help
```

If the command is unavailable, use the portable Skill runtime launcher. Inside
an ARC source checkout, the shared package virtual environment is a direct
development fallback:

```bash
<skill-dir>/scripts/arc-runtime arc-jobs --help
packages/arc-paper/.venv/bin/arc-jobs --help
```

`<skill-dir>` means the directory containing the active ARC Skill.
Use the selected launcher in place of `arc-jobs` in later examples. Do not
search package internals for another executable.

## Inspect or Validate an Existing Run

Keep the exposed run root and returned run ID together, then run:

```bash
arc-jobs status --run-root <run-root> --run-id <run-id>
arc-jobs validate --run-root <run-root> --run-id <run-id>
```

Prefer the owning package's status or validation command when it exposes the
package-specific information needed for the task. Use `arc-jobs` for the
generic durable-run view or when no owning command exists.

All non-help commands emit one `arc.command_result.v2` JSON envelope. Always
check top-level `status`, `warnings`, and `error`. The most useful result paths
are:

| Operation | Result path | Meaning |
| --- | --- | --- |
| `status` | `data.run.status` | Durable lifecycle such as `pending`, `running`, `paused`, `failed`, or `succeeded` |
| `status` | `data.run.can_resume` | Whether the durable run may be resumed by its owner |
| `status` | `data.run.result` | Verified result artifact ID and returned relative path, or `null` |
| `status` | `data.run.error` | Latest durable failure, or `null` |
| `status` | `data.run.resume` | Durable pause descriptor, or `null` |
| `status` | `data.run.working_state` | Exact returned paths for editable recovery state |
| `validate` | `data.valid` | Whether durable state and referenced artifacts validate |
| `validate` | `data.issues[]` | Validation issue `code`, `message`, and path components |

`status` is a read-only query, so its top-level `status` is normally
`completed` even when `data.run.status` is `failed` or `paused`. Read the nested
durable lifecycle before deciding what to do. `validate` also completes as a
query; use `data.valid`, not only the process exit code, as the validation
answer.

## Request a Cooperative Stop

Request a stop only when the user or owning workflow intends to pause work:

```bash
arc-jobs stop \
  --run-root <run-root> \
  --run-id <run-id> \
  --reason "<reason>"
```

Inspect `data.run.stop_requested` to confirm that the request was recorded and
`data.run.status` for the lifecycle observed while recording it. A stop is
cooperative; the owner may need time to reach a safe boundary.

Do not stop a run only because a model call is slow or temporarily quiet.
Outside an explicit user stop, compare successive public snapshots and request
a stop only for a recorded recurring error or repeated lack of goal-directed
progress.

`arc-jobs` has no resume command. Resume through the owning package with the
same run root, run ID, and any input required by its pause descriptor.

## Failed-Run Recovery

`failed` records the latest failed attempt; it is not necessarily permanent.
The generic status response reports `data.run.can_resume` and these exact
run-relative recovery paths:

| Recovery material | Result path |
| --- | --- |
| Semantic input | `data.run.working_state.semantic_input` |
| Working index | `data.run.working_state.index` |
| Published working artifacts | `data.run.working_state.artifacts` |
| Editable candidates | `data.run.working_state.candidates` |
| Latest error record | `data.run.working_state.last_error` |

Use only paths returned for the selected run. When a filesystem read is
authorized, resolve a returned path beneath
`<run-root>/runs/<run-id>/`; do not discover a run by scanning physical durable
state or infer its identity from directory names.

A trusted agent may edit current semantic input, artifacts, or candidates, or
delete an editable file to request regeneration. Preserve ARC-managed
immutable objects, recovery snapshots, indexes, and locks. When changing an
upstream file, delete downstream editable files that must be recomputed. ARC
warns about possible stale state but does not maintain a dependency
invalidation graph or retry automatically. After correction, resume through
the owning package; `arc-jobs` remains the generic inspector.

## Markdown Report Export

For user-facing Markdown, run the project-aware PDF renderer described in
`rules/math_typeset.md` as an ordinary blocking command instead of routing it
through `arc-jobs`. The Markdown remains editable workflow source and the PDF
is the visible human delivery. On failure, print `WARNING:` with the exact
error and preserve workflow state, but do not claim PDF delivery. Rendering
failure does not change scientific status or handoff eligibility. Do not debug
Pandoc or TeX as part of the research workflow unless the user explicitly asks
for typesetting diagnosis.

## Help

```bash
arc-jobs --help
arc-jobs <command> --help
```

Help describes current flags. Keep the run root and ID exposed by the owning
workflow or supplied to its direct-root command; never reconstruct them from
physical durable-state paths. There is no generic fixture-creation command:
creating an ownerless run would not provide valid package semantics or a resume
path.
