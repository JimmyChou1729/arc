# ARC Jobs

`arc-jobs` is ARC's protocol-neutral control surface for a durable run that is
already owned by an ARC package. It does not submit commands, launch detached
workers, list global jobs, watch progress, or export results.

## Durable Run Control

Use an explicit run root and run ID:

```bash
arc-jobs status --run-root <run-root> --run-id <run-id>
arc-jobs stop --run-root <run-root> --run-id <run-id> [--reason TEXT]
arc-jobs validate --run-root <run-root> --run-id <run-id>
```

- `status` returns the current durable snapshot as one command-result envelope.
- `stop` records a resumable stop request. Resume through the owning package
  workflow; `arc-jobs` has no generic resume command.
- `validate` checks durable state and reports typed issues without changing it.

Start `arc-domain build`, `arc-companion build`, and other owning commands
directly. Use their package-specific status/resume/stop/validate commands when
available. When a direct command should run without blocking the conversation,
use the coding-agent host's background-command facility and keep the command
portable across hosts.

Do not stop a run merely because it is slow. LLM calls use the idle-timeout
policy documented in `manuals/arc-llm.md`; a stop pauses the current attempt
for same-run resume.

## Markdown Report Export

Convert a report to PDF with the canonical Pandoc/XeLaTeX command in
`rules/math_typeset.md` (PDF Export section), run as an ordinary command
instead of routing it through `arc-jobs`. On failure, print `WARNING:` with the
exact error and continue according to the owning workflow; do not debug Pandoc
or TeX unless the user requested that work.
