# ARC Proposer-Reviewer Quick Start

`arc-proposer-reviewer` runs typed proposer and reviewer batches and exposes
verified committed rounds. Use it for bounded idea comparison or blind
multi-agent review. It delegates model calls to `arc-llm` and durable execution
to `arc-jobs`.

## Run ARC Proposer-Reviewer

This is a core-only command: the ARC plugin has no standalone bin wrapper.
Invoke it through the Skill runtime launcher and use that launcher in every
example below:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer --help
```

`<skill-dir>` means the directory containing the active ARC Skill.
Do not search package internals for another executable.

## Prepare the Smallest Batch

Save this complete, validated v7 request as `<batch-request.json>`. It defines
one proposer, one reviewer, one loop, and one round:

```json
{
  "schema_version": "arc.proposer_reviewer.batch.v7",
  "batch_id": "comparison-1",
  "loops": [
    {
      "loop_id": "question-1",
      "context": {
        "question": "What is the strongest supported answer?"
      },
      "proposers": [
        {
          "worker_id": "proposer-1",
          "instructions": "Propose a concise, evidence-aware answer.",
          "output_schema": {
            "type": "object",
            "additionalProperties": false,
            "required": ["answer"],
            "properties": {
              "answer": {"type": "string"}
            }
          },
          "model": {
            "provider": "auto",
            "model": null,
            "tier": "medium"
          }
        }
      ],
      "reviewer": {
        "worker_id": "reviewer-1",
        "instructions": "Review the proposal and return actionable feedback.",
        "output_schema": {
          "type": "object",
          "additionalProperties": false,
          "required": ["assessment"],
          "properties": {
            "assessment": {"type": "string"}
          }
        },
        "model": {
          "provider": "auto",
          "model": null,
          "tier": "medium"
        }
      },
      "max_rounds": 1,
      "allow_early_stop": true,
      "on_proposer_failure": "fail_loop",
      "review_final_round": true,
      "revision_context_mode": "feedback_only",
      "input_ids": null
    }
  ],
  "inputs": [],
  "failure_policy": "collect"
}
```

The v7 request is closed. Keep every shown field, including `inputs` and
`input_ids`. Worker `output_schema` values describe the proposer's result and
the reviewer's payload; ARC supplies the surrounding reviewer decision and
feedback contract.

## Validate and Run

Validation is local and makes no provider call:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer validate \
  --request <batch-request.json>
```

Check `data.valid`, `data.schema_version`, `data.batch_id`, and
`data.loop_count`. Then start the durable batch:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer run \
  --request <batch-request.json> \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id>
```

All non-help commands emit one `arc.command_result.v2` envelope. Always inspect
top-level `status`, `warnings`, `error`, and `resume`. Persist the exact batch
identity at `run.id`; `run.revision` is the returned durable revision. During
execution, `data.run.status` is the durable lifecycle,
`data.run.can_resume` reports recoverability, and `data.run.result` is the
verified final batch-result reference when the run succeeds.

Use the public query commands below to read scientific work. Do not reconstruct
results from provider sessions, partial files, or physical durable-state paths.

## Inspect Progress and Committed Work

Inspect batch and loop lifecycle:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer inspect \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id>
```

Important paths are:

- `data.inspection.durable_lifecycle` and
  `data.inspection.lifecycle_counts` for batch state;
- `data.inspection.loops[]` for loop projections;
- `data.inspection.loops[].loop_id`, `.lifecycle`, `.phase`,
  `.current_round`, and `.rounds_completed` for one loop;
- `data.inspection.loops[].activity` for best-effort activity;
- `data.inspection.loops[].pause` for actionable worker pauses.

`inspect` is a read-only query, so its top-level `status` is normally
`completed`; use `data.inspection.durable_lifecycle` for the durable state.

Read only verified committed-round references with `trace`:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer trace \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id>
```

Trace data is at `data.trace`. Loop entries are at `data.trace.loops[]`; each
entry has `loop_id` and committed `rounds[]`. For each round, inspect
`round_number`, `proposal_refs`, `review_ref`, and `transcript_refs`.

Expand one committed round through the public reader:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer show-round \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --loop-id <loop-id> \
  --round <round-number>
```

The expansion is at `data.round`. Its principal fields are `loop_id`,
`round_number`, `proposals`, `review`, `proposal_refs`, `review_ref`, and
`transcript_refs`. `proposals` and `review` contain the verified committed
scientific envelopes; the reference fields retain provenance. `show-round` is
the public expansion of one verified committed-round reference.

To obtain lifecycle and trace together, add `--include-trace` to `inspect`.
The same trace then appears at `data.trace`. If trace integrity fails,
`data.trace` is `null` and top-level `warnings[]` contains
`trace_integrity_error`; the lifecycle projection remains available.

## Resume a Paused Batch

An execution pause reports a descriptor at top-level `resume`. The public
inspection also exposes worker pauses under
`data.inspection.loops[].pause.entries[]`, including `reason`, `input_required`,
`resume_key`, `response_contract`, `request_ref`, and `resume_action`.

When input is required under `arc.llm.resume_input.v3`, begin with this closed
skeleton:

```json
{
  "schema_version": "arc.llm.resume_input.v3",
  "resume_key": "copy-from-resume.resume_key",
  "action": "continue",
  "host_response": null,
  "candidate_digest": null,
  "reason": null
}
```

Every field is required even when `null`. Copy the selected pause's
`resume_key` exactly. Choose the action required by its response contract and
request: `continue` may carry a `host_response`, `replace` requires a non-empty
`reason`, and `accept_candidate` requires a returned `candidate_digest`. Leave
unrelated fields as `null`.

Resume the same batch so committed rounds and completed model calls are reused:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer resume \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --input <resume-input.json>
```

Omit `--input` only when the pause says no input is required. Fix external
provider authentication, quota, or availability before resuming a pause caused
by that condition.

## Request a Cooperative Stop

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer stop \
  --run-root <project-dir>/proposer-reviewer \
  --run-id <stable-run-id> \
  --reason "<specific observed reason>"
```

Inspect `data.stop_requested` and `data.durable_lifecycle` in the response.
A stop pauses the current attempt and preserves its durable frontier for
same-run resume.

Interactive workers do not have an arbitrary tool-turn quota. Normal research
may take a long time. Compare public `inspect` snapshots and stop cautiously
only when successive snapshots show the same recurring failure or repeated
turns with no concrete contribution toward the requested scientific objective.
Elapsed time, temporary silence, and pipe activity alone are not stop
conditions. Record the compared evidence and reason, and always honor an
explicit user stop. Scientific proposer-reviewer rounds remain finite and
configurable; do not continue merely because configured rounds remain.

## Help

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer --help
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer <command> --help
```

Help describes current commands and flags. Use the complete v7 batch and v3
resume templates above for their closed JSON contracts.
