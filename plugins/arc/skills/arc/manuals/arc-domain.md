# ARC Domain Quick Start

`arc-domain` builds a durable research-domain package from one seed paper and
a scientific intent. Use it for a field summary, citation graph, evidence
pack, paper pack, or browsable network. Use the managed domain workflow when a
request needs multiple seeds, origin selection, project-local exports, or a
handoff to idea generation.

Every command returns a typed JSON envelope. Check top-level `status`,
`warnings`, and `error` before using the fields under `data`.

## Run ARC Domain

Examples below assume `arc-domain` is on `PATH`. Check once with:

```bash
arc-domain --help
```

If unavailable, use the portable Skill launcher. Inside an ARC source checkout,
the package virtual environment is a direct development fallback:

```bash
<skill-dir>/scripts/arc-runtime arc-domain --help
packages/arc-paper/.venv/bin/arc-domain --help
```

Use the selected launcher in place of `arc-domain` below. Do not search package
internals for another executable.

## Build and Read One Domain

This concrete example builds a domain around quasi-single-field inflation:

```bash
arc-domain build arXiv:0911.3380 \
  --intent "Map the mechanisms, observables, and current theoretical limits of quasi-single-field inflation" \
  --project-dir local/quasi-single-field-domain \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown
```

Set `--host-authority` once per run. Use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value on resume. For `restricted` or `unknown` host requests, follow
`manuals/arc-llm.md`; do not assume a universal broker.

On a completed, published build, retain these exact values:

| Identity | JSON result path | Purpose |
| --- | --- | --- |
| Durable run ID | `run.id` | Inspect, resume, stop, or validate this attempt |
| Stable domain ID | `data.domain.id` | Read or export the active published domain |

Use those returned values in the next commands:

```bash
arc-domain status --project-dir local/quasi-single-field-domain \
  --run-id <run.id>
arc-domain status --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id>

arc-domain get-summary --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id>
arc-domain get-graph --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id>
```

`get-summary` returns the verified summary object at `data.summary` and the
active generation at `data.domain.active`. `get-graph` returns the graph at
`data.graph`. A run status reports its snapshot at `data.run` and progress at
`data.progress`; a domain status additionally reports the latest and active
generation under `data.domain`.

For a date-bounded field request, the managed workflow freezes `as_of_date`
and passes the corresponding `recent_window_days`. The date window applies to
the citer corpus; it does not silently replace the canonical origin.

## Materialize the Five Public Exports

`get-summary` and `get-graph` read JSON into the command envelope. Use
`materialize-export` to copy any active verified export to a new explicit file:

```bash
arc-domain materialize-export \
  --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id> --name summary \
  --output local/quasi-single-field-domain/exports/summary.md

arc-domain materialize-export \
  --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id> --name graph \
  --output local/quasi-single-field-domain/exports/graph.json

arc-domain materialize-export \
  --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id> --name network \
  --output local/quasi-single-field-domain/exports/network.html

arc-domain materialize-export \
  --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id> --name evidence-pack \
  --output local/quasi-single-field-domain/exports/evidence-pack.json

arc-domain materialize-export \
  --project-dir local/quasi-single-field-domain \
  --domain-id <data.domain.id> --name paper-pack \
  --output local/quasi-single-field-domain/exports/paper-pack.json
```

Each result reports the written path, SHA-256 digest, and byte count at
`data.export.output`, `data.export.digest`, and `data.export.size_bytes`.
`data.export.source_run_id` identifies the active generation copied. Existing
output files are never overwritten.

## Recovery, Resume, and Validation

```bash
arc-domain status --project-dir local/quasi-single-field-domain \
  --run-id <run.id>
arc-domain resume <run.id> \
  --project-dir local/quasi-single-field-domain \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown
arc-domain validate <run.id> --project-dir local/quasi-single-field-domain
arc-domain stop <run.id> --project-dir local/quasi-single-field-domain \
  --reason "operator requested stop"
```

Status exposes `data.run.status`, `data.run.can_resume`, `data.run.error`,
`data.run.resume`, and the stable paths under `data.run.working_state`. Resume
the same run after a pause, interruption, failure, or stop. Add
`--input '<ResumeInput JSON object>'` only when the typed resume descriptor
requires a response.

One bounded automatic retry exists: a structurally valid domain-summary model
response that fails package identity or evidence-coverage validation receives
one fresh semantic retry with validation feedback. If that retry is also
machine-invalid, ARC pauses with both attempts and an editable candidate. This
retry repairs unusable machine output; it does not revise valid scientific
judgment.

Other failed-run recovery is explicit. Inspect `data.run.error` and the
returned `working_state` paths, correct a candidate or artifact to adopt it, or
delete the relevant working file to regenerate it, then resume. If an upstream
semantic input changes, remove downstream working files that should be
rebuilt. ARC does not automatically classify those recovery choices. A stop is
resumable, not a failed replacement run, and the previously active domain
generation remains published until a recovered build fully validates.

After every attempt, read status, warnings, and published artifact references
before deciding whether to deliver, recover, or resume.

Durable state lives below `<project-dir>/.arc/domain/`. Without
`--paper-cache-root`, paper data uses `ARC_PAPER_CACHE` when set and otherwise
`.arc/cache/arc-paper` below the launch directory. Keep the working directory
stable or pass one explicit local cache on both build and resume.

## Managed Workflow Semantics

The managed workflow records visible summary warnings and publishes
`arc.workflow.domain_manifest.v4` only after verified exports. The manifest
references the content-addressed `arc.workflow.domain_seed_provenance.v1`
artifact and preserves every domain package before an Ideas workflow starts.
Pairwise `domain_relationships` are advisory evidence with confidence and
warnings; they never choose an Ideas route. If relationship analysis is
unavailable, the manifest remains usable and Ideas proceeds from package cards
with a visible warning.

## Help

```bash
arc-domain --help
arc-domain build --help
arc-domain materialize-export --help
arc-domain <command> --help
```

Use command help for policy controls, strict date windows, model selection,
project state, local paper-cache selection, output names, and exact flags.
