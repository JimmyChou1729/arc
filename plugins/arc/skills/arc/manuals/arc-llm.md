# ARC LLM Quick Start

`arc-llm` runs one durable, structured host-model task. Most research work
should call its owning ARC package or workflow instead. Use `arc-llm` directly
for provider diagnosis, a bounded structured request, or same-run recovery.
Proposer-reviewer orchestration belongs to `arc-proposer-reviewer`.

## Check the Provider

```bash
arc-llm doctor --provider auto
```

The diagnostic reports safe availability and selection facts without printing
credentials. `auto` chooses a supported host-native provider when available.

## Generate Structured Output

Prepare a closed request such as:

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

Then start and inspect the durable run:

```bash
arc-llm generate \
  --request <request.json> \
  --run-root <project-dir>/.arc/llm \
  --host-authority <host-authority> \
  --run-id <stable-run-id>

arc-llm status \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id>
```

Persist the returned run ID and read the typed result body. The run root is
caller-owned durable state, not a temporary directory.

For `repair: "format"`, ARC first applies its normal local/formatter recovery.
If that still cannot produce a valid structured result, it preserves the raw
candidate and formatter record and performs one complete fresh generation of
the original task with validation feedback. A second invalid result pauses for
supervision; it does not trigger another formatter or silently discard earlier
workflow results. Provider authority, quota, and availability pauses remain
separate from this bounded output recovery.

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value for every resume of that run.

With `restricted` or `unknown` authority, provider output uses ARC's host-turn
contract. If the model requests a host action and the runtime holder did not
explicitly supply a broker, the request becomes a durable manual pause. ARC
does not ship or assume a production universal broker. Read the typed pause,
perform only authorized work, and resume the same run with the requested input.

## Resume or Stop

```bash
arc-llm resume \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id> \
  --host-authority <host-authority> \
  --input <resume-input.json>

arc-llm stop \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id> \
  --reason "<reason>"
```

Omit `--input` only when the pause descriptor says no input is required.
Resume the same run rather than starting an unrelated retry. Authentication,
quota, and unavailable-provider pauses require the external condition to be
fixed first.

## Help

```bash
arc-llm --help
arc-llm <command> --help
```

The CLI emits one typed JSON result for non-help commands. Model selection and
input artifacts are request data; ARC copies each verified input into the
provider workspace.
