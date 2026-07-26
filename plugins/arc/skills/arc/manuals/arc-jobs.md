# ARC Jobs Quick Start

`arc-jobs` inspects durable runs already created by another ARC package. Use it
when an owning command returned a run root and run ID but has no more specific
control command. It does not create or resume package work.

## Inspect or Validate an Existing Run

```bash
arc-jobs status --run-root <run-root> --run-id <run-id>
arc-jobs validate --run-root <run-root> --run-id <run-id>
```

`status` returns the current typed snapshot. `validate` checks durable state
without changing it. Prefer the owning package's status or validate command
when one exists.

Request a stop only when the user or owning workflow intends to pause work:

```bash
arc-jobs stop \
  --run-root <run-root> \
  --run-id <run-id> \
  --reason "<reason>"
```

Resume through the owning package with the same run ID. Do not stop a run only
because a model call is slow or temporarily quiet. Outside an explicit user
stop, compare successive public snapshots and request a cooperative stop only
for a recorded recurring error or repeated lack of goal-directed progress.

## Markdown Report Export

For user-facing Markdown, run the project-aware PDF renderer from
`rules/math_typeset.md` as an ordinary blocking command instead of routing it
through `arc-jobs`. The Markdown remains editable workflow source; the visible
PDF is the human delivery. On failure, print `WARNING:` with the exact error
and preserve workflow state, but do not claim delivery until the PDF exists.
Do not debug Pandoc or TeX as part of the research workflow unless the user
explicitly asks for typesetting diagnosis.

## Help

```bash
arc-jobs --help
arc-jobs <command> --help
```

All commands return one typed JSON result. Keep the run root and ID from the
owning command; never infer them from physical durable-state paths.
