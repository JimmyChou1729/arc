# Build Domain Workflow

Use this workflow to build project-local research-domain references from one or
more seed papers.

## Inputs

Read `<project-dir>/context.json`. Use the exact values from that file for all
ARC calls, especially `user_intent`, `seed_paper_list`, `llm_provider`,
`model`, `model_tier`, `workers`, and the closed domain-build policy. Do not
pass a citation provider, citation query, sort/filter, or refresh option: the
domain builder is fixed to INSPIRE and owns those internal choices. Default
`recent_window_days` to `365`. Without a policy document, the CLI resolves the
current UTC `as_of_date` and all default policy values before persistence.
For "the last two years", record the exact day count back to the corresponding
calendar date two years earlier.

### Phase 1: Prepare Project Artifacts

Step 1: Create `<project-dir>/domain/`.

Step 2: Preserve `<project-dir>/context.json` as the workflow source of truth.
Do not substitute a paraphrased intent string into ARC calls.

### Phase 2: Build Domain Caches

Distinct ARC domain ids may build concurrently. Do not run duplicate builds for
the same domain id in parallel; see `manuals/arc-domain.md`.

Step 1: Resolve the domain id for each `<seed-paper>` with the exact
`<user-intent>`. If multiple entries resolve to the same domain id, keep one
entry for Phase 2 and record the duplicate in `<project-dir>/context.json` or a
visible workflow note.

Step 2: For each distinct `<seed-paper>` in `seed_paper_list`, start one
durable domain build. `arc-domain build` owns its own `arc-jobs` run; do not
wrap it in `arc-jobs submit`.

```bash
arc-domain build <seed-paper> \
  --intent "<user-intent>" \
  --llm-provider <llm_provider> \
  --model <model> \
  --model-tier <model_tier> \
  --workers <workers> \
  --recent-window-days <recent_window_days>
```

Use exact `--model` only when the context intentionally pins one model.
When supplied, `--policy '<full-policy-json-document>'` must be a complete
JSON policy document. Explicit policy flags override it; the CLI persists the
complete resolved policy. There is no `--refresh` or `--as-of-date` flag.

If several domain IDs are distinct, their builds may run concurrently. Record
every returned `run_id` and `domain_id`. A paused result must be continued only
with `arc-domain resume <run-id>`. When its resume descriptor requires input,
pass the matching document with `--input '<resume-input-json-document>'`. Inspect progress with
`arc-domain status --run-id <run-id>`, cancel with `arc-domain cancel <run-id>`,
and validate with `arc-domain validate <run-id>`.

Step 3: Inspect each `arc.command_result.v1` body. Do not treat an exit code
alone as success. Continue only when every build has succeeded and published an
active export generation. If any build is paused, failed, or cancelled, print
`WARNING:` with the run ID and stop before exporting project-local artifacts.

For domain package boundaries and `paper_json_pack.json`, see
`manuals/arc-domain.md`.

### Phase 3: Copy Domain Artifacts

Step 1: Derive a safe file prefix:

```bash
arc-paper safe-dir-name <seed-paper> --json
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
`<project-dir>/<seed-safe>_domain_summary.md`. This
report-export gate is not satisfied until `md2pdf` has been started or a
`WARNING:` with the exact blocker is recorded. Do not wait for PDF completion.
If PDF generation appears bugged, report it and continue this workflow; do not
debug or fix PDF generation unless the user explicitly asks.

Do not generate, attach, or copy separate single-paper LLM summaries for the
foundation paper or best-reference paper as part of the domain build. The
domain summary should mention both papers briefly instead.

Step 4: After all distinct domain artifacts have been copied, write the
project-local domain handoff manifest:

Before running the helper, record each successful build in
`<project-dir>/context.json` under `domain_records` as objects containing the
requested `seed_paper` and returned `domain_id`. Do not substitute the selected
foundation paper for the requested seed; they may differ.

```bash
python3 <skill-dir>/workflows/scripts/write-domain-manifest.py \
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

### Phase 4: Scope Boundary and Interactive Review

Show the completed domain artifact and manifest paths. If domain construction
was the requested outcome, report them and stop in either automation level.
When the caller explicitly requested a downstream workflow, `interactive`
mode pauses here before entering it; `auto` continues without asking unless a
warning or failure occurred. Continue only to a workflow the caller requested
or that `SKILL.md` identifies as a prerequisite. `auto` does not authorize idea
generation or otherwise expand scope.
