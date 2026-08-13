# ARC LLM Quick Start

`arc-llm` runs one durable, structured host-model task. Most research work
should call its owning ARC package or workflow instead. Use `arc-llm` directly
for provider diagnosis, one bounded structured request, or same-run recovery.
Proposer-reviewer orchestration belongs to `arc-proposer-reviewer`.

## Run ARC LLM

Examples below assume `arc-llm` is on `PATH`. Check once with:

```bash
arc-llm --help
```

If the command is unavailable, use the portable Skill runtime launcher. Inside
an ARC source checkout, the shared package virtual environment is a direct
development fallback:

```bash
<skill-dir>/scripts/arc-runtime arc-llm --help
packages/arc-paper/.venv/bin/arc-llm --help
```

`<skill-dir>` means the directory containing the active ARC Skill.
Use the selected launcher in place of `arc-llm` below. Do not search package
internals for another executable.

## Check the Provider

```bash
arc-llm doctor --provider auto
```

The diagnostic reports safe availability and selection facts without printing
credentials. Inspect `data.provider`, `data.available`, `data.executable`, and
`data.details`. `auto` chooses a supported host-native provider when one is
available.

## Prepare a Structured Request

Save this complete closed request as `<request.json>` and change the task,
prompt, or output schema as needed:

```json
{
  "schema_version": "arc.llm.request.v4",
  "task_id": "bounded_assessment",
  "prompt": "Return a concise structured assessment.",
  "output": {
    "kind": "json",
    "schema": {
      "type": "object",
      "additionalProperties": false,
      "required": ["answer"],
      "properties": {"answer": {"type": "string"}}
    },
    "repair": "format"
  },
  "model": {"provider": "auto", "model": null, "tier": "medium"},
  "session": null,
  "inputs": []
}
```

The request is closed: keep every shown top-level field, including nullable
and empty fields. Model selection and verified input artifacts are request
data; ARC copies each accepted input into the provider workspace.

## Generate and Read the Result

Use a caller-owned durable root, not a temporary directory:

```bash
arc-llm generate \
  --request <request.json> \
  --run-root <project-dir>/.arc/llm \
  --host-authority <host-authority> \
  --run-id <stable-run-id>
```

All non-help commands emit one `arc.command_result.v2` envelope. Always inspect
top-level `status`, `warnings`, `error`, and `resume`. Persist the exact run
identity at `run.id`; `run.revision` is the returned durable revision.

For a successful generation:

| Result | JSON path |
| --- | --- |
| Durable lifecycle | `data.run.status` (`succeeded`) |
| Result artifact ID and path | `data.run.result.artifact_id`, `data.run.result.path` |
| Public command artifacts | `artifacts[]` |
| Result artifact entry | item in `artifacts[]` whose `role` is `result` |

The model value is stored in the verified result artifact; it is not copied
inline into the command envelope. Retrieve it only from the location formed
from returned values:

```text
<run-root>/runs/<run.id>/<data.run.result.path>
```

The matching `artifacts[]` entry returns the same relative `path`. Use that
exact returned path for the selected run. Do not enumerate the object store,
search manifests, guess an `accepted/result.*` name, or infer provenance from
physical cache layout.

Every invalid JSON candidate gets at most one complete fresh generation of the
original task with validation feedback. For `repair: "format"`, ARC first uses
its normal local or formatter recovery and regenerates only when that cannot
produce a valid result. For `repair: "strict"` or `"local"`, it proceeds
directly to the one fresh generation. A second invalid result pauses for
supervision; it does not start an unbounded retry loop or silently discard
earlier workflow results. Raw candidates and formatter records remain durable.

## Inspect Status and Pauses

```bash
arc-llm status \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id>
```

`status` is a query, so top-level `status` is normally `completed`. Read the
actual lifecycle from `data.run.status`. Other important paths are
`data.run.error`, `data.run.can_resume`, `data.run.result`, and
`data.run.resume`.

An execution command that pauses reports the actionable descriptor at
top-level `resume`; a later `status` query reports the stored equivalent at
`data.run.resume`. Inspect:

- `resume.reason`, `resume.resume_key`, and `resume.input_required`;
- `resume.input_schema` for the required input contract;
- `resume.request_artifact` and `resume.details` for the specific request.

Resolve a returned request-artifact path through the same selected run root and
run ID. Do not search durable state for a plausible request.

## Resume or Stop

When `resume.input_required` is true and `resume.input_schema` is
`arc.llm.resume_input.v3`, start from this closed skeleton:

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

Every field is required even when its value is `null`. Copy
`resume.resume_key` exactly. Choose the action required by the pause descriptor
and request artifact: `continue` may carry a `host_response`, `replace` requires
a non-empty `reason`, and `accept_candidate` requires one returned
`candidate_digest`. Leave fields unrelated to that action as `null`.

Then resume the same durable run:

```bash
arc-llm resume \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id> \
  --host-authority <host-authority> \
  --input <resume-input.json>
```

Omit `--input` only when the descriptor says `input_required: false`. Fix
authentication, quota, or provider availability before resuming a pause caused
by that external condition. Do not start an unrelated retry under a new run ID.

Request a cooperative stop only when intended:

```bash
arc-llm stop \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id> \
  --reason "<reason>"
```

Inspect `data.run.status` in the response, then use `status` for subsequent
lifecycle changes. An active run receives a cooperative request; a run that is
already terminal remains terminal.

## Authority and Concurrency

Set `<host-authority>` once per run. Use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value on every resume.

With `restricted` or `unknown` authority, provider output uses ARC's host-turn
contract. If the model requests a host action and the runtime holder did not
explicitly supply a broker, the request becomes a durable manual pause. ARC
does not ship or assume a production universal broker. Perform only authorized
work and resume with the requested input.

For provider-heavy parallel work, interpret **max parallel** as a target of
100 concurrent provider calls. Configure both the caller's worker pool and
`ProviderGateOptions.global_limit` to create and admit that demand. This is not
a hard ceiling on an explicit numeric request. Effective concurrency can be
lower when demand is lower, available memory reaches ARC's guard threshold, a
provider limit applies, or the provider circuit opens after failures or rate
limiting. On Linux, cgroup admission treats `memory.stat`'s inactive file cache
as reclaimable; active cache and reclaimable slab remain counted as used.

If the caller exposes an `arc-jobs` work-group ID, use `arc-jobs workers set`
to change its pending-work target without restarting; see `manuals/arc-jobs.md`.
That operation changes caller demand only. It does not raise or bypass the
provider gate, memory guard, or circuit breaker.

## Help

```bash
arc-llm --help
arc-llm <command> --help
```

Help describes current commands and flags. Use the complete request and resume
templates above for their closed JSON contracts.
