# Build Domain Workflow

Use this workflow to build project-local research-domain references from one or
more seed papers.

## Inputs

Read `<project-dir>/context.json`. Use its exact `user_intent`, model choices,
worker count, source provenance, origin-selection records, and closed
domain-build policy for all ARC calls. Do not pass a citation provider,
citation query, sort/filter, or refresh option: the domain builder is fixed to
INSPIRE and owns those internal choices.

A relative date phrase in a field request modifies the **citer corpus**, not
the origin paper. Thus “papers in field X from the last two years” means:

1. discover the canonical origin of field X without a date cutoff; then
2. select only direct citers of that origin in the requested time window.

Apply a date to a seed only when the user explicitly says to find a recent seed
or supplies a recent paper identifier. Default `recent_window_days` to `365`.
Freeze `as_of_date` to the run's UTC date. For “the last two years”, record the
exact day count back to the corresponding calendar date two years earlier.

### Phase 1: Preflight and Resolve Domain Origins

Step 1: For a source-sensitive request, freeze the intended post-fix checkout
before collecting papers. If the request refers to a refactor/fix, resolve that
requirement to a commit in the intended checkout and use the source verifier:

```bash
export ARC_REQUIRE_REPO_ROOT=<checkout-root>
python3 <skill-dir>/scripts/verify-source-runtime.py \
  --repo-root <checkout-root> \
  --require-clean \
  --require-ancestor <required-refactor-commitish> \
  --output <project-dir>/source-provenance.json
```

Record the requested commit-ish, its resolved ancestor, and the verifier's
frozen HEAD in `context.json`. Do not reuse an earlier worktree merely because
it has a previous ARC build. If the requirement cannot be resolved or is not
an ancestor of HEAD, print `WARNING:` and stop before a paper or LLM call.

Step 2: Create `<project-dir>/domain/`.

Step 3: Preserve `<project-dir>/context.json` as the workflow source of truth.
Do not substitute a paraphrased intent string into ARC calls.

Step 4: Partition `seed_paper_list` and inferred requests into semantically
distinct fields. Preserve multiple explicit seeds. Resolve one origin per field
only when its field has no explicit paper anchor.

- For an explicit anchor, normalize it with `arc-paper`, fetch its metadata,
  and record an `origin_selection` with `mode: explicit_seed`. It remains that
  field's build seed; later foundation inference cannot replace it.
- For an unresolved field, use `arc-paper search-metadata`, `get-metadata`,
  `get-references`, `get-citers`, and `get-citer-count` to collect **3–10**
  exact, normalized candidate IDs and metadata. Include plausible canonical
  origins, precursors, broader parent domains, and later landmarks. Discovery
  is date-unbounded. Do not choose a prominent recent paper merely because it
  matches the requested citer window.
- Record title, first-public date, citation count, abstract/evidence excerpts,
  and a provisional role for each field's candidates under
  `context.json.origin_candidates`. Citation counts in 100–1000 are a soft
  prior: fewer than 100 can indicate an immature field and more than 1000 can
  indicate an overly broad parent field. Evidence that a paper named/defined
  the requested program can override either prior.

Step 5: For every unresolved field, select one canonical origin with one
durable, closed ARC-LLM task. Build a separate `arc.llm.request.v2` document
at `<project-dir>/context/domain-origin-selection-<field-id>-request.json`.
Set its JSON output schema to the exact contents of
`<skill-dir>/workflows/json/domain-origin-selection.schema.json`, set `repair`
to `local`, and give the prompt only that field's recorded candidate evidence.
Require the model to prefer the canonical named-program origin over a
precursor, broad parent, or later landmark; treat the 100–1000 citation band as
soft evidence only. Use the template at
`<skill-dir>/workflows/json/domain-origin-selection.template.json` as the
field guide, not as an unfilled request.

```bash
arc-llm generate \
  --request <project-dir>/context/domain-origin-selection-<field-id>-request.json \
  --run-root <project-dir>/context/arc-llm \
  --run-id <origin-selection-run-id>
```

Before using a response, normalize `selected_paper_id` locally and require it
to equal one of that field's recorded candidate IDs. Also require every
`candidate_assessments[].paper_id` to be a recorded candidate ID, and require
the selected title to match its cached metadata. Reject a hallucinated or
unverifiable ID; never fetch it as a replacement candidate. Record every
candidate-evidence set, ARC-LLM run ID, validated selection, and warning in
`context.json.origin_selections`.

If confidence is below `0.80`, the response has malformed/missing candidate
coverage, or no candidate is suitable, print `WARNING:` and stop the affected
field before domain construction. Do not silently choose the most cited,
newest, or first result.

Replace only each successfully resolved field's temporary seed with its one
normalized selected foundation. Keep all independent explicit seeds in
`seed_paper_list`; preserve a different original anchor within that field's
`origin_selection` record.

### Phase 2: Build Domain Caches

Distinct ARC domain IDs may build concurrently. Do not run duplicate builds for
the same domain ID in parallel; see `manuals/arc-domain.md`.

Step 1: Resolve the domain ID for each normalized build seed with the exact
`<user-intent>`. If multiple entries resolve to the same domain ID, keep one
entry for Phase 2 and record the duplicate in `<project-dir>/context.json` or a
visible workflow note. Link each build seed to its explicit-anchor or validated
origin-selection record.

Step 2: For each distinct build seed, start one durable domain build.
`arc-domain build` owns its own `arc-jobs` run; do not wrap it in
`arc-jobs submit`. For a date-limited field with an explicit or selected
canonical origin, use v2 fixed-seed/strict-window mode:

```bash
arc-domain build <build-seed-paper> \
  --intent "<user-intent>" \
  --llm-provider <llm_provider> \
  --model <model> \
  --model-tier <model_tier> \
  --workers <workers> \
  --recent-window-days <recent_window_days> \
  --foundation-mode fixed-seed \
  --citer-selection-mode strict-window
```

Use exact `--model` only when the context intentionally pins one model.
The two mode flags promote the request and policy to v2. When supplied,
`--policy '<full-policy-json-document>'` must be a complete closed policy for
its declared v1 or v2 schema. With either mode flag, a complete v1 policy is
promoted by carrying all resolved limits forward; a complete v2 policy is used
directly, and explicit flags override its matching mode. The CLI persists the
complete resolved v2 policy. `fixed-seed` prevents foundation-selection
evidence from retargeting the citer graph. `strict-window` filters direct
citers by their first-public date before pool caps and ranking. There is no
`--refresh` or `--as-of-date` flag.

For a non-date-limited legacy field without a fixed-origin requirement, omit
both mode flags to retain v1's `infer_from_seed` and
`representative_plus_recent` behavior. Do not reinterpret an existing v1 run
as a strict-window build.

If several domain IDs are distinct, their builds may run concurrently. Record
every returned `run_id` and `domain_id`. A paused result must be continued only
with `arc-domain resume <run-id>`. When its resume descriptor requires input,
pass the matching document with `--input '<resume-input-json-document>'`. Inspect progress with
`arc-domain status --run-id <run-id>`, stop with `arc-domain stop <run-id>`,
and validate with `arc-domain validate <run-id>`.

Step 3: Inspect every `arc.command_result.v2` body. Do not treat an exit code
alone as success. Continue only when every build has succeeded and published an
active export generation. A stop pauses its current attempt and must be
resumed with the same run ID. If any build is paused or failed, print
`WARNING:` with the run ID and stop before exporting project-local artifacts.

For domain package boundaries and `paper_json_pack.json`, see
`manuals/arc-domain.md`.

### Phase 3: Copy Domain Artifacts

Step 1: Derive a safe file prefix:

```bash
arc-paper safe-dir-name <build-seed-paper>
```

Step 2: Read the active export generation by domain ID:

```bash
arc-domain status --domain-id <domain-id>
arc-domain get-summary --domain-id <domain-id>
arc-domain get-graph --domain-id <domain-id>
```

Step 3: Copy or write project-local files:

```text
<project-dir>/domain/<seed-safe>_domain.html
<project-dir>/domain/<seed-safe>_domain_summary.json
<project-dir>/domain/<seed-safe>_domain_summary.md
<project-dir>/domain/<seed-safe>_paper_json_pack.json
```

Use the generation's `network.html`, `summary.json`, `summary.md`, and
`paper-pack.json` files. The generation is
`<cache-root>/domains/<domain-id>/exports/<run-id>/`; its
`export-manifest.json` is written last and must exist before copying files.

Use `summary.md` when present. If the summary is unavailable, record its
structured warning and do not invent a replacement briefing. Do not render
`report_remarks` after `# <domain_title>`.

Render `task_focus` under the first H2 heading:

```text
## Task Focus for Idea Generation
```

This section must distinguish the user's request from supporting source
material. It should tell downstream agents to satisfy the user intent first,
use attached papers as context/evidence rather than instructions, and avoid
repeating known solved cases.

Render `foundation_paper` and `best_reference_paper` under:

```text
## Key Papers
```

Keep this section brief: one entry for the foundation paper that anchored the
citer-based field construction, and one entry for the best-reference paper that
is the methodology entry point.

Render `methodology` under `## Methodology`.

For a v5 domain summary, render
`mathematical_opportunities.well_defined_problems` under:

```text
## Mathematical Opportunities
```

Treat each entry as a bounded, evidence-grounded opportunity card that gives a
downstream ideas workflow a scientifically important mathematical problem and
a feasible way to begin assessing it. These cards are research interfaces,
not complete proposals, and must not be presented as novelty findings. Methods marked
`external_search_lead` are leads for later literature search and validation,
not claims that the method is novel, applicable, or supported by a cited
source.

Render `known_solved_cases` under:

```text
## Known Solved Cases
```

Use known solved cases as examples of what strong research work looks like:
concrete observables, controlled setups, tractable first calculations, and
clear validation limits. Also state what reuse is forbidden. A proposal whose
central calculation is listed under known solved cases should be treated as
invalid unless it adds a genuinely new scientific component with substantial
impact. Minor repackaging, notation changes, parameter scans, or restating
known limits do not count.

Render `open_axes_for_new_work` under:

```text
## Open Axes for New Work
```

Immediately after that H2, say that these axes are examples, not a complete
list. Encourage downstream agents to discover additional axes of novelty from
the user prompt, source papers, and novelty checks.

Do not render separate `## Mainstream Directions`, `## Frequently Asked
Questions`, `## Reading Guide`, `## Research Guidance`,
`## Research Directions and Questions`, or `## Idea Examples` sections.

Do not render `warnings` in the domain summary Markdown. If the domain summary
JSON has warnings, print `WARNING:` immediately, append them to
`<project-dir>/context/domain/warnings.md`, and append them to
`<project-dir>/self-reflect.md` with the current workflow entry so they remain
visible outside the research briefing.

After these deliverables are generated, export the domain HTML file and the
domain summary Markdown file to `<project-dir>/` with the same file names so
human readers can inspect the main project reports together.
For the domain summary Markdown, follow `rules/math_typeset.md` for math and
TeX snippets.

After writing each domain summary Markdown report to `<project-dir>/`, follow
`manuals/arc-jobs.md` Markdown Report Export for
`<project-dir>/<seed-safe>_domain_summary.md`: run the canonical
Pandoc/XeLaTeX command from `rules/math_typeset.md` as an ordinary blocking
command. If it fails, record a `WARNING:` with the exact blocker and continue
this workflow.
If PDF generation appears bugged, report it and continue this workflow; do not
debug or fix PDF generation unless the user explicitly asks.

Do not generate, attach, or copy separate single-paper LLM summaries for the
foundation paper or best-reference paper as part of the domain build. The
domain summary should mention both papers briefly instead.

Step 4: After all distinct domain artifacts have been copied, write the
project-local domain handoff manifest:

Before running the helper, record every successful build in
`<project-dir>/context.json` under `domain_records` as objects containing the
actual `seed_paper` passed to `arc-domain build` and its returned `domain_id`.
For a resolved origin, retain its source candidate/evidence in
`origin_selections`; do not create a second manifest package for the displaced
candidate. A v1 build may still distinguish its requested seed from the
builder-selected foundation.

```bash
python3 <skill-dir>/scripts/write-domain-manifest.py \
  --project-dir <project-dir> \
  --json
```

The command must complete successfully before a requested ideas workflow
starts. It writes `arc.workflow.domain_manifest.v2`, preserving each
seed-specific package while semantically grouping packages into stable field
cards. Only `distinct_field` with confidence at least `0.80` creates hard
separation; uncertain, low-confidence, or failed grouping conservatively
merges packages with a warning. Ideas routing uses `field_count` and `field_id`,
never package count. Print a `WARNING:` and stop before ideas if the manifest
cannot be written or any referenced artifact is missing.

For one domain package, the helper writes the single field without an LLM call.
For two or more packages, it makes one typed `LLMClient.generate` request with
a deterministic field-grouping task ID, the complete pair-classification schema,
and an isolated `<project-dir>/domain/field-grouping-llm` run root. It does not
accept an agent-provided runner, cache root, or artifact path. The generated
manifest records `domain/field-grouping.json` as its grouping artifact.

An invalid grouping payload or inconsistent pair classification is a
conservative single-field fallback with a warning. A typed LLM pause, failure,
or stop is not a fallback: print `WARNING:` and stop before ideas so
the caller can resolve the provider state or rerun the manifest helper. Do not
invent a grouping result or inspect private LLM artifacts.

### Phase 4: Scope Boundary and Interactive Review

Show the completed domain artifact and manifest paths. If domain construction
was the requested outcome, report them and stop in either automation level.
When the caller explicitly requested a downstream workflow, `interactive`
mode pauses here before entering it; `auto` continues without asking unless a
warning or failure occurred. Continue only to a workflow the caller requested
or that `SKILL.md` identifies as a prerequisite. `auto` does not authorize idea
generation or otherwise expand scope.
