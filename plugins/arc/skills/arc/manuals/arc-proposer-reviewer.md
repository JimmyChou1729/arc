# ARC Proposer-Reviewer Quick Start

`arc-proposer-reviewer` runs typed proposer and reviewer batches and exposes
their verified committed rounds. Use it for bounded idea comparison or blind
multi-agent review when an ARC workflow has prepared a batch request. It uses
`arc-llm` for model calls and `arc-jobs` for durable execution.

The plugin has no standalone bin wrapper for this core-only command. Invoke it
through the Skill runtime launcher:

## Validate and Run a Batch

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer validate \
  --request <batch-request.json>

<skill-dir>/scripts/arc-runtime arc-proposer-reviewer run \
  --request <batch-request.json> \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id>
```

Validation is local. Persist the run root and returned run ID before observing
or resuming the batch.

## Inspect Committed Work

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer inspect \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id>

<skill-dir>/scripts/arc-runtime arc-proposer-reviewer show-round \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --loop-id <loop-id> \
  --round <round-number>
```

`inspect` reports lifecycle and best-effort activity. Use `trace` to list only
verified committed-round references; `show-round` is the public expansion of
one committed proposal/review envelope. Never reconstruct results from
sessions, partial files, or physical durable-state paths.

## Stop

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer stop \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --reason "<specific observed reason>"
```

Interactive workers do not have an arbitrary tool-turn quota. Normal research
may take a long time. Compare public `inspect` snapshots and stop cautiously
only when interaction turns continue without useful progress, failures recur,
or the provider is no longer advancing the task. A stop pauses the current
attempt and preserves its durable frontier for same-run resume.

## Resume

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer resume \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --input <resume-input.json>
```

Omit `--input` only when the pause descriptor requires no input. Resume the
same run so committed rounds and completed model calls are reused.

## Help

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer --help
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer <command> --help
```

Use help for the current request contract, query options, trace output, and
typed failure details.
