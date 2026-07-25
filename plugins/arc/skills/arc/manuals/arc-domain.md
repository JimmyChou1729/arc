# Arc Domain Package

`arc-domain` builds a durable research-domain generation from one seed paper
and an optional scientific intent. A completed generation contains the graph,
network HTML, paper JSON pack, evidence pack, and optional domain summary.

The domain identity is derived only from the normalized seed paper and trimmed
intent. The durable run identity also includes the fully resolved policy and
LLM model selection. A v2 policy records whether that seed may be inferred
away from (`infer_from_seed`) or is the fixed citer anchor (`fixed_seed`), and
whether citers use legacy representative selection or a strict date window.

## Scope And Data Boundary

Domain citation data is fixed to INSPIRE. There is no citation-provider
abstraction, provider selector, query syntax, sort/filter flag, or refresh
flag. The builder uses the fixed INSPIRE `mostrecent` and `mostcited` citer
orderings internally, then interleaves and bounds that candidate pool locally.

`arc-domain` uses the concrete `ArcPaperService` through its domain access
layer. Metadata and references are separate operations. A graph paper's full
text is parsed at most once while building a pack; its table of contents and
conclusion/outlook projection use that same parsed document.

## Foundation and Citer Modes

The original closed policy/request documents remain
`arc.domain_build_policy.v1` and `arc.domain_build_request.v1`. They replay the
legacy behavior: `infer_from_seed` foundation choice and
`representative_plus_recent` citer selection. Existing v1 runs remain valid and
must not be reinterpreted.

Passing either mode flag creates the closed v2 policy/request documents:

```text
foundation_mode: infer_from_seed | fixed_seed
citer_selection_mode: representative_plus_recent | strict_window
```

Use `fixed_seed` when a workflow has independently selected the canonical
origin. Foundation audit/reference evidence remains available for context, but
it cannot retarget the citer graph. Use `strict_window` when the request means
“papers that cite this foundation in the last N days.” The builder classifies
each direct citer by first-public date before merging, capacity limits, and LLM
ranking; a valid non-arXiv date qualifies, a later revision date does not, and
an unknown date is excluded with a structured count/warning. Foundation and
context/reference nodes may predate the window, but selected `domain_paper`
nodes may not. An empty eligible window produces a successful
foundation/context graph with a warning.

The foundation candidate citation band 100–1000 is a configurable soft prior,
not a hard eligibility rule. Low counts can signal a shallow field and very
high counts a broad parent domain; canonical-origin evidence can override both.
The fully resolved policy records `as_of_date` and `recent_window_days`. For a
two-year request, the latter is the exact day count to the corresponding
calendar date two years earlier.

## Build And Resume

### Phase 1: Start a Build

Step 1: Use the default v1 policy or prepare a complete closed policy document.
Without `--policy`, the CLI resolves the current UTC date and the default
limits before creating the durable run.

Step 2: Run one full build.

```bash
arc-domain build <seed-paper> \
  --intent "<user-intent>" \
  --recent-window-days 365 \
  --citer-pool-limit 1000 \
  --ranked-paper-limit 50 \
  --graph-node-limit 90 \
  --llm-provider auto \
  --model <optional-model> \
  --model-tier medium \
  --workers 8 \
  --cache-root <optional-cache-root>
```

When supplied, `--policy '<full-policy-json-document>'` must be a complete
closed v1 policy JSON document. Explicit policy flags override its four
selectable limits; the persisted request always contains the complete resolved
policy.
`--run-id` may name the durable run explicitly. Build output uses the
`arc.command_result.v1` envelope and reports the run and domain IDs.

Step 3: For a fixed canonical origin and a strict citer time window, promote to
v2 explicitly:

```bash
arc-domain build <canonical-origin-paper> \
  --intent "<user-intent>" \
  --recent-window-days <window-days> \
  --foundation-mode fixed-seed \
  --citer-selection-mode strict-window \
  --llm-provider auto \
  --model-tier medium
```

CLI spelling uses hyphens; persisted policy values use underscores. A supplied
`--policy` must always be complete for its declared v1 or v2 schema. With
either mode flag, the CLI promotes a complete v1 policy by carrying its
resolved limits forward; it uses a complete v2 policy directly, and a flag
overrides only its matching mode. `--foundation-mode` and
`--citer-selection-mode` are the only mode controls; do not use a generic
filter/sort option.

Do not use `init`, `identify-foundation`, `build-network`, `build-evidence`,
`build-paper-json-pack`, `summarize`, or any `llm-*` alias. They are not
supported command surfaces.

### Phase 2: Resume, Inspect, or Cancel

Step 1: Resume only the same durable run after a pause.

```bash
arc-domain resume <run-id>
arc-domain cancel <run-id>
arc-domain validate <run-id>
```

If the paused envelope reports `input_required: true`, pass the matching
`arc_llm` resume document as inline JSON:

```bash
arc-domain resume <run-id> --input '<resume-input-json-document>'
```

Step 2: Inspect a run or the latest run for one domain.

```bash
arc-domain status --run-id <run-id>
arc-domain status --domain-id <domain-id>
```

`status --domain-id` resolves the catalog's `latest` run. `get-summary` and
`get-graph` resolve the catalog's successfully published `active` generation:

```bash
arc-domain get-summary --domain-id <domain-id>
arc-domain get-graph --domain-id <domain-id>
```

Successful, paused, cancelled, and query commands exit zero; a failed run exits
one and invalid CLI input exits two.

## Durable Artifacts And Exports

Run-local immutable artifacts are replayed by fixed logical artifact IDs. A
completed build materializes one export generation only after its source
artifacts verify. The export manifest is written last.

```text
<cache-root>/runs/
<cache-root>/domains/<domain-id>/catalog.json
<cache-root>/domains/<domain-id>/exports/<run-id>/
  graph.json
  network.html
  paper-pack.json
  evidence-pack.json
  summary.json          # optional
  summary.md            # optional
  export-manifest.json  # written last
```

The catalog contains only `latest` and `active`. `latest` is the newest created
run; `active` advances only after a complete export generation is published.
A delayed older run cannot replace a newer active generation. Publication can
be retried without rerunning a succeeded durable build.

## Failure Semantics

Seed metadata, the foundation citer pool, and final artifact publication are
essential. Per-paper pack, reference, and noncritical metadata failures are
collected as structured warnings. An LLM pause pauses the outer build and is
resumed through the run ID. Exhausted transport or timeout failures use the
documented deterministic fallback for foundation/network work, or make the
summary unavailable with a warning. Authentication, quota, rate-limit, and
provider-unavailable conditions remain paused for external resolution.

The owning domain workflow prints and records summary warnings to project `self-reflect.md` and `context/domain/warnings.md`; the briefing itself must not conceal them.

That workflow writes `arc.workflow.domain_manifest.v2` only after a completed
domain export and validates its referenced artifacts before any ideas workflow.
For multiple exported packages, its `write-domain-manifest.py` helper uses one
typed `LLMClient.generate` pair-classification request with a deterministic task
ID and a project-local `domain/field-grouping-llm` run root. Invalid grouping
content degrades conservatively to one field with a warning; a typed pause,
failure, or cancellation stops the handoff instead. The helper has no legacy
runner, controller, or private-artifact reading surface.

## MCP Status

ARC-owned MCP integration is retired. Use the durable `arc-domain` CLI above;
there is no legacy domain MCP compatibility surface.
