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
  --host-authority unknown \
  --run-id <stable-run-id>

arc-llm status \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id>
```

Persist the returned run ID and read the typed result body. The run root is
caller-owned durable state, not a temporary directory.

## Resume or Stop

```bash
arc-llm resume \
  --run-root <project-dir>/.arc/llm \
  --run-id <stable-run-id> \
  --host-authority unknown \
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
provider workspace. `--host-authority unrestricted` is valid only when the
invoking host has explicitly granted unrestricted authority. Otherwise use
`unknown` (the portable default), which requests host turns when needed.
