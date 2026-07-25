# ARC LLM

`arc-llm` is ARC's durable host-LLM task runner. Normal research workflows
should use their owning package or workflow, such as `arc-domain`; use this
manual for a bounded structured task, provider diagnosis, or durable recovery.

The CLI writes one `arc.command_result.v2` JSON document to stdout. Do not add
`--json`. Its only command surface is `generate`, `resume`, `status`, `stop`,
and `doctor`.

## Source-Sensitive Preflight

For a development run that must use a particular checkout, set
`ARC_REQUIRE_REPO_ROOT=<checkout-root>`. Workflow scripts then reject installed,
marketplace-cached, and other-checkout ARC modules.

When the request also names a refactor or fix, resolve its commit in the
intended checkout, require it to be an ancestor of the frozen HEAD, and record
the provenance before any durable call. Do not substitute an earlier observed
checkout merely because it is already available.

```bash
<skill-dir>/scripts/arc-runtime setup --profile core
export ARC_REQUIRE_REPO_ROOT=<checkout-root>
python3 <skill-dir>/scripts/verify-source-runtime.py \
  --repo-root <checkout-root> \
  --require-clean \
  --require-ancestor <required-refactor-commitish> \
  --output <project-dir>/source-provenance.json
```

The resulting record contains the resolved required ancestor and the clean,
frozen `git.head`. If a requested refactor cannot be mapped to a commit, stop:
the run cannot truthfully claim that it used that refactor. `verify-source-runtime.py`
re-executes with the Skill runtime before loading checkout modules.

## Provider Diagnosis

Run the one supported diagnostic command before debugging a provider:

```bash
arc-llm doctor --provider auto
```

`auto` selects a host-native provider when one is detected. The diagnostic
returns provider availability and safe configuration facts; it does not print
credentials or provider configuration values. Provider diagnosis belongs only
to the one command shown above; package-specific and nested diagnostic forms
are unsupported.

Do not diagnose `arc-llm` by running `pip show arc-llm` in the system Python.
`arc_llm` is supplied by the ARC checkout/runtime; an import failure normally
means the wrong Python path/runtime, not a PyPI installation problem.

## Model Selection and Kimi

Model tiers are `low`, `medium`, `high`, and `xhigh`. Use `medium` unless the
owning workflow has a documented reason to choose another tier.

The `kimi` provider uses an ACP adapter. Its provider configuration is
inherited, so ARC cannot claim host-configuration isolation for it. ARC starts
ACP sessions without MCP servers and denies ACP reverse permission, filesystem,
and terminal requests; this is a bounded protocol surface, not a general host
sandbox. The adapter supports native session resume, embeds the JSON Schema in
the prompt for structured output, and records partial usage only when the ACP
response supplies it.

## Durable Structured Task

### Step 1: Write a complete request document

`generate` accepts one closed `arc.llm.request.v2` document. It has a stable
`task_id`, non-empty `prompt`, `model`, `session`, `capabilities`, `inputs`, and
an `output` contract. For JSON output, use `kind: "json"`, a Draft 2020-12
schema, and `repair: "format"` unless the caller requires local-only repair or
strict rejection.

```json
{
  "schema_version": "arc.llm.request.v2",
  "task_id": "bounded-review",
  "prompt": "Return the requested structured assessment.",
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
  "capabilities": {
    "internet": false,
    "inherit_host_config": false,
    "allowed_tools": []
  },
  "inputs": []
}
```

Validate all schema-required fields locally before calling the provider. A
request with an exact model needs an explicit provider and the default tier.
`format` first performs deterministic local JSON repair. If substantive content
still does not satisfy the schema, ARC makes one isolated formatting call with
the same provider/model, no tools or internet, and no original-worker replay.
Use `local` to prohibit that extra call, or `strict` to prohibit all repair.
Formatter usage is recorded in the generation's formatting artifact; transient
formatter availability pauses remain resumable without input.

### Step 2: Generate and inspect

```bash
arc-llm generate \
  --request <request.json> \
  --run-root <project-dir>/context/arc-llm \
  --run-id <stable-run-id>

arc-llm status \
  --run-root <project-dir>/context/arc-llm \
  --run-id <stable-run-id>
```

The run root belongs to the caller and is durable state, not a temporary file.
Persist the returned run ID and inspect the command-result body rather than
treating an exit code alone as success.

For Codex JSONL execution, ARC treats the requested output-last-message as the
sole authoritative terminal candidate. Earlier `item.completed` events are
bounded diagnostic evidence only; they must not create a second model call or
silently override the final message. ARC applies the declared local schema
validation/repair before reporting completion.

### Step 3: Resume or stop only that run

```bash
arc-llm resume \
  --run-root <project-dir>/context/arc-llm \
  --run-id <stable-run-id> \
  --input <resume-input.json>

arc-llm stop \
  --run-root <project-dir>/context/arc-llm \
  --run-id <stable-run-id> \
  --reason "superseded by corrected evidence"
```

Omit `--input` only when the paused run does not require one. A resume input is
a complete `arc.llm.resume_input.v2` document tied to the returned resume key.
Never retry by creating a same-purpose unrelated run: durable replay and resume
preserve provider accounting and the audit trail.

## Failure and Capability Boundaries

Transport and temporary provider failures may be resumable; authentication,
quota, and unavailable-provider failures remain paused for external resolution.
Stop pauses the current attempt; resume continues the same durable run. A failed command exits one;
invalid CLI/request input exits two.

The request capability policy is explicit. Normal ARC workflows leave internet
access, inherited host configuration, and host tools disabled unless the owning
workflow has a documented need. Provider diagnostics and durable artifacts are
audit information, never a credential store.

## Core Proposer-Reviewer Batches

`arc-proposer-reviewer` is the core package and CLI for typed proposer-worker
and reviewer batches. It owns the batch protocol and committed dialogue
frontier; `arc-llm` supplies individual model calls and `arc-jobs` supplies
durable execution. It is not an `arc-llm` CLI alias.

For a plugin or standalone Skill, invoke the core-only tool through the
runtime launcher:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer inspect \
  --run-root <run-root> --run-id <run-id>
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer trace \
  --run-root <run-root> --run-id <run-id>
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer show-round \
  --run-root <run-root> --run-id <run-id> --loop-id <loop-id> --round <number>
```

These commands are read-only and make no model call. `inspect` is available
for pending, running, paused, and terminal batches. It reports run and loop
lifecycle, phase, current round, a safe pause summary, and activity counts.
Activity is explicitly best effort: never use it for ranking, recovery,
retry, or resume decisions.

`trace` returns only complete rounds atomically committed by each loop, with
verified logical artifact IDs and content digests. A published-but-uncommitted
partial round is never visible. The returned run revision and per-loop revision
vector identify this observation without claiming a globally linearized
snapshot. `show-round` is the only public query that expands a committed
round's proposal and review JSON.

The projection never exposes sessions, task IDs, private group IDs, full pause
records, or physical paths. A corrupt committed frontier fails closed for
`trace` and `show-round`; `inspect --include-trace` keeps the basic inspection
and reports a trace-integrity warning instead. Do not infer results by reading
the durable directory layout.

## Workflow Interaction Contracts

The ideas workflow does not call `arc-llm` recursively or expose its workers to
shell, ARC CLI, MCP, cache-administration, or arbitrary-path tools. It gives
each configured proposer and reviewer one typed interactive JSON contract owned
by `arc-proposer-reviewer`. The contract permits at most two automatic evidence
interaction rounds; a third request pauses durably for explicit resume handling.

The ideas batch shares one 24-request ARC-paper resolver across every worker
and round. See `workflows/ideas.md` for its exact operation allowlist and for
the public `BatchRequest` and committed-round observation contract. The
resolver is the workflow's only evidence surface; it does not enable nested
model calls or an `arc-llm` CLI callback.
