# Arc Domain Package

`arc-domain` builds a durable research-domain generation from one seed paper
and an optional scientific intent. A completed generation contains the graph,
network HTML, paper JSON pack, evidence pack, and optional domain summary.

The domain identity is derived only from the normalized seed paper and trimmed
intent. The durable run identity also includes the fully resolved policy and
LLM model selection.

## Scope And Data Boundary

Domain citation data is fixed to INSPIRE. There is no citation-provider
abstraction, provider selector, query syntax, sort/filter flag, or refresh
flag. The builder uses the fixed INSPIRE `mostrecent` and `mostcited` citer
orderings internally, then interleaves and bounds that candidate pool locally.

`arc-domain` uses the concrete `ArcPaperService` through its domain access
layer. Metadata and references are separate operations. A graph paper's full
text is parsed at most once while building a pack; its table of contents and
conclusion/outlook projection use that same parsed document.

## Build And Resume

### Phase 1: Start a Build

Step 1: Use the default policy or prepare a complete closed policy document.
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
closed policy JSON document. Explicit policy flags override its four selectable
limits; the persisted request always contains the complete resolved policy.
`--run-id` may name the durable run explicitly. Build output uses the
`arc.command_result.v1` envelope and reports the run and domain IDs.

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

## MCP Status

`arc-mcp` is not migrated with this domain interface. Do not use legacy domain
MCP tools as an alternative to the CLI above. See
`docs/architecture/core-refactor-downstream-breakage.md` for the explicitly
deferred adapter migration.
