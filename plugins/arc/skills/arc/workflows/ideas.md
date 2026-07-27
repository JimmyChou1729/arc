# Ideas Workflow

Use this workflow for Case 2 idea generation. It selects the single-domain or
cross-domain variant from the project domain manifest, then runs concurrent
proposer-reviewer loops. Each loop has exactly one proposer and exactly one
reviewer; the reviewer serves only that proposer and may commit up to the
template's three rounds by default.

## Inputs

Read `<project-dir>/context.json`. Use the exact `user_intent`.
Use `skill_dir` from context as `<skill-dir>` in commands below.
If `<project-dir>/context.json` is missing, return to `SKILL.md` Phase 1 before
idea generation. If it exists but lacks `automation_level`, initialize that
field to `auto` in place without asking an execution-mode question.
Do not synthesize ideas manually.

### Phase 1: Prepare Config

Step 1: Create `<project-dir>/.arc/ideas/` for operational state. The ranking
publisher creates the visible `<project-dir>/ideas/` PDF archive only after a
successful run.

Step 2: Copy
`workflows/json/ideas.config.template.json` to:

```text
<project-dir>/.arc/ideas/<run-id>.config.json
```

Step 3: Replace `<run-id>`, `<project-dir>`, `<user_intent>`, and
`<skill-workflow-json-dir>`.

Set `domain_manifest_path` to
`<project-dir>/.arc/domain/domain-manifest.json`. Current manifest v3, including
its validated `arc.workflow.domain_seed_provenance.v1` artifact, routes by
`field_count`: one field, including multiple seed-specific packages, uses the
single-domain prompts; two or more fields use cross-domain prompts, directed
transfer profiles as optional lenses, and reviewer scientific assessment.
Cross-domain cards and source/target roles use `field_id`. A missing,
unsupported, or invalid manifest must be regenerated before any ideas work.

Proceed only when the domain-build handoff status is `completed` or `degraded`.
For a degraded handoff, print its warnings and use only the verified domain
material; a failed or paused handoff must be resolved before idea generation.

Step 4: Keep `variant_glob` as `ideas-*.variant.json`. The release package
runs only enabled variants, then selects only the enabled variant applicable
to the manifest; it must not run both the single-domain and cross-domain
variants for the same request.

Step 5: Keep the shipped proposer and reviewer `model_tier` values at `high`
for normal idea generation unless the user requests another quality/cost tier.

Step 6: Keep `loops_per_variant` at `5` unless the run should use a different
number of concurrent instances for each setup. Cross-domain runs ship with
five distinct exploration profiles. Single-domain runs derive distinct routes
in stable manifest, package, and list order: first from validated
`open_axes_for_new_work`, then from deduplicated
`mathematical_opportunities.well_defined_problems`, then from a finite set of
general theoretical-physics exploration lenses. Each route is passed to the
proposer as `exploration_profile`, but every profile is an optional,
non-exhaustive lens rather than an assignment, required taxonomy, or coverage
quota. The exact user intent remains primary; a proposer may leave its lens
when a stronger minimal direct route lies elsewhere, and it need not absorb
every example, method, or observable mentioned by the profile. Before
finalizing, proposer and reviewer apply a removal counterfactual and prefer the
shortest minimally sufficient setup and core calculation that remain
well-defined, feasible, testable, genuinely novel, and consequential. Separable
methods, validations, observables, and applications become optional follow-ons.
The reviewer continues to use the common marking scheme. If an explicit set is
used, top-level `exploration_profiles` must contain exactly one profile object
per loop for either research scope. If the automatic single-domain sources
cannot supply enough distinct routes, provide an explicit set; never create
duplicate loops that differ only by ID.

### Phase 2: Run Ideas

Step 1: Run:

```bash
python3 <skill-dir>/scripts/run-ideas.py \
  --config <project-dir>/.arc/ideas/<run-id>.config.json \
  --host-authority <host-authority> \
  --json
```

LLM calls have no default runtime or inactivity timeout. The foreground runner
streams batch, loop, round, worker, and available provider-message progress
JSON to stderr. Keep its terminal session active; theoretical research can take
a long time, so do not interrupt merely because the run is slow or quiet.

If one loop or worker is clearly slower than its peers or than the scientific
task reasonably suggests, or if several loops fail, inspect the public
snapshot:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer inspect \
  --run-root <project-dir>/.arc/ideas \
  --run-id <run-id>
```

Compare snapshots rather than treating elapsed time alone as failure. Inspect
the active worker, last activity, pause information, and sanitized failure
causes. Be patient when these show normal scientific progress. Stop only when
successive snapshots show the same recurring errors or repeated interaction
without a concrete contribution toward the requested scientific objective:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer stop \
  --run-root <project-dir>/.arc/ideas \
  --run-id <run-id> \
  --reason "<specific observed reason>"
```

Use stop cautiously, cite the compared snapshots, and record the concrete
reason. Elapsed time, temporary silence, and pipe activity alone are not stop
decisions. Always honor an explicit user stop. `SIGINT` and `SIGTERM` remain
available for the foreground process. A stop pauses the current attempt for
same-run resume.

Step 2: Treat the runner result as the public batch handoff. It materializes
one `BatchRequest` and executes it through `ProposerReviewerHandler` in the
`RunRepository` rooted at `<project-dir>/.arc/ideas`, with the stable run ID
`<run-id>`. The request is published under the logical artifact ID
`proposer-reviewer/request`. Do not inspect or derive behavior from a run's
private directories, loop state, transcripts, sessions, task IDs, group IDs,
or physical artifact paths.

The result's `batch` object reports the run revision, loop revision vector, and
whether the committed trace verified. `inspect_batch` supplies lifecycle and
best-effort activity; `read_batch_trace` supplies only atomically committed
round references; `read_batch_round` is the only public expansion of a
proposal and its review envelope. The scientific review is the envelope's
`payload`, not a separate intermediary artifact. If committed-trace verification
fails, print its `WARNING:` and do not rank or reconstruct output from files.
If the executor raises after writing durable progress, the result status is
`failed`, its typed `execution_error` identifies the boundary failure, and the
loop and committed-round fields still report the verified durable frontier.

Step 3: Print any returned `WARNING:` messages. Rank only loops whose public
lifecycle is `succeeded`; failed, pending, running, paused, and
integrity-error loops remain visible in inspection but are excluded from the
formal ranking. When no loop is formally rankable but the trace is valid and
contains at least one complete committed proposer-reviewer round,
`run-ideas.py` automatically attempts to publish a non-formal provisional
`partial-ideas.pdf`. Rendering failure adds a warning and never changes the
real batch lifecycle. The partial report does not resume work, adopt
uncommitted provider files, or alter scientific scores, ranks, or candidate
visibility. For loop concurrency and durable pause/resume behavior, see
`manuals/arc-proposer-reviewer.md`.

### Research Tools

ARC and web search are complementary research surfaces. Workers may use the
web, ARC paper tools, and the shared ARC paper cache when those capabilities
are available and useful. They should use focused checks rather than exhaustive
searching, inspect the strongest candidates first, and record each actual
source or query with a short result. If a host or provider cannot supply a
capability, state the limitation once and continue from the available evidence.

The broader novelty review remains the primary assessment. The
citation-neighborhood audit below is a required supplementary signal, never a
replacement for broader web search, INSPIRE metadata search, shared-cache
search, scientific comparison, or other appropriate prior-art checks. Complete
those broader checks regardless of whether the citation scan is complete or
finds a direct hit, and base novelty and confidence on the combined evidence.
A no-hit citation result alone must not raise either score. For cross-domain
ideas, the broader review still requires independent source-domain,
target-domain, and intersection checks.

For each new idea nucleus, the first reviewer round performs a bounded INSPIRE
citation-neighborhood audit. Select one canonical paper that defines the
baseline problem plus at most two prior-art papers nearest the proposed novelty
delta. These papers need not be domain seeds; do not scan every paper cited by
the proposer. For each selected paper, run `arc-paper get-citer-count` first,
then `arc-paper search-citers` with `--scan-limit 1000`, `--limit 50`, and
specific multiword phrases and synonyms spanning the idea's background,
mechanism, and observable.

Read the returned shortlist abstracts and newest/most-cited controls before
opening primary records or full text for suspected direct overlaps. Reuse a
completed scan in later rounds while the idea nucleus, baseline paper, and
novelty delta remain unchanged; otherwise update only the affected scans.
Record the selected baseline or nearest paper, total and scanned citer counts,
`scan_complete`, matched papers, and exclusion reasons in `evidence_checked`.
Record the exact ARC commands and terms actually used in `tool_queries_used`.
Use those same existing arrays to record the actual broader novelty sources,
results, and queries as well as the citation-neighborhood evidence; do not
report a citation-only audit as a completed novelty review.

No direct match supports only “no direct precedent found in this citation
neighborhood,” never a proof of novelty. INSPIRE unavailability, a neighborhood
larger than 1000 citers, incomplete coverage, or missing abstracts must remain
visible as a warning and lower novelty confidence while the reviewer continues
with the other available novelty checks. None of these conditions, nor the
citation-neighborhood audit itself, removes an idea, changes its score or rank,
or hides it from the report.

Set `<host-authority>` to `unrestricted` only when the host explicitly reports
unrestricted permissions; otherwise set it to `unknown` and reuse that value
when resuming the same run. Under `restricted` or `unknown`, a model host
request becomes a durable manual pause when the runtime holder did not
explicitly supply a broker. ARC does not assume a production universal broker;
inspect the typed pause and resume the same run with the required input. This
workflow does not define a paper-operation allowlist, a tool ledger, or a
host-specific fallback. The runtime holder supplies the authority attestation
and any explicit broker; the LLM service selects the corresponding safe mode.

Final ranked ideas must come from `run-ideas.py`'s public committed batch data
and the read-only ranking helper, not ad-hoc agent judgment. Keep every
trace-verified candidate visible with its marks, assessment, limitations, and
revision feedback. In cross-domain mode, the reviewer distinguishes genuine,
substantive transfers from decorative or currently under-specified ones, but a
fixable translation, setup, or execution error remains actionable feedback
rather than erasing the direction. Recommend replacement only when no
minimally sufficient repair preserves a genuine and consequential transfer.
The source domain may contribute a mature method or mechanism without itself
receiving a new result.

In single-domain mode, prioritize an important target-domain problem that is
mathematically well-defined and has an executable systematic route.
Cross-disciplinary transfer is entirely optional: a proposer may consider or
use a mature method from another field when it is scientifically useful, but no
idea, loop, or batch is required to include one, there is no interdisciplinary
quota, and interdisciplinary framing receives no ranking reward. Judge
same-domain and cross-disciplinary candidates by the same scientific criteria.
If a proposal does import an external method, make its structure, required
adaptation, applicability conditions, validation checks, and kill criterion
concrete; only then should the reviewer validate the source area, target
domain, intersection, and shortlisted source papers. Otherwise record the
external method as not used and do not request cross-disciplinary evidence.
Judge feasibility and problem importance explicitly in marks, assessment, and
revision feedback. A repairable error in an otherwise substantive nucleus
should produce a concrete next-round fix, while a convenient but low-value
exercise or an important problem without any feasible minimally sufficient
formulation should score accordingly.

### Phase 3: Inspect Artifacts

Report the durable repository root and run ID, not internal run-layout paths:

```text
run root: <project-dir>/.arc/ideas
run ID: <run-id>
request artifact: proposer-reviewer/request
```

Step 1: Use the public observation APIs after the run completes. `inspect_batch`
may show a lifecycle for any batch state; the strict trace exposes only complete
committed rounds. The ranking helper selects the highest-ranked committed round
for each succeeded loop, rather than assuming the final round is best, while
preserving the scientific assessment and revision feedback for every complete
candidate. It emits logical artifact IDs and content digests, never physical
proposal or review paths.

After the batch completes, run one post-batch portfolio-level scientific
assessment by default. This is a single advisory over the portfolio, not
another scoring pass. It is holistic and free-topic: a common core shared by
several ideas is only one possible finding, not a required section, taxonomy, or
template. The advisory may identify a minimal direct direction omitted by the
loops or another omission outside the ranking, but it must label every such
item as unranked and novelty-unassessed until a normal novelty review is
performed. It never changes proposer or reviewer marks, selected rounds,
scores, rank order, or candidate visibility.

Portfolio-assessment unavailability, failure, or malformed output adds a
`WARNING:` and does not block the deterministic ranked report. The ranking
helper remains read-only: it does not invoke this assessment, mutate the batch,
or reinterpret its results. It may only preserve or render advisory content
that the completed run supplies through its public handoff.

The run result contract is `arc.workflow.ideas.result.v4`. The formal JSON
ranking contract is `arc.ideas.selected_rounds.v7`; partial rankings use
`arc.ideas.partial_selected_rounds.v3`. Read scientific `status`
separately from `durable_lifecycle`: a durable batch may finish successfully
while the scientific status is `degraded` because one or more loops failed.
The current contracts have no `run_lifecycle` alias.

Publish the deterministic ranked report as PDF to both the per-run archive and
the easy-to-find project root:

```bash
python3 <skill-dir>/scripts/rank-ideas.py \
  --project-dir <project-dir> \
  --run-id <run-id> \
  --format pdf
```

This writes editable Markdown only under
`<project-dir>/.arc/ideas/reports/<run-id>/` and atomically publishes
`<project-dir>/ideas/<run-id>/ranked-ideas.pdf` plus
`<project-dir>/ranked-ideas.pdf`. The PDF paths are the human delivery.
Follow the `manuals/arc-jobs.md` Markdown report export procedure: if PDF
rendering fails, print a `WARNING:` and do not claim that a ranked-ideas
delivery was published.

For a paused or otherwise incomplete batch with trace-verified complete
committed rounds, a diagnostic report may also be published explicitly:

```bash
python3 <skill-dir>/scripts/rank-ideas.py \
  --project-dir <project-dir> \
  --run-id <run-id> \
  --mode partial \
  --format pdf
```

`--mode formal` remains the default and continues to rank only succeeded
loops. Partial mode uses every complete committed proposer-reviewer round
visible in the verified trace, preserves the same scoring and candidate
visibility policy, and marks its title, metadata, and ordering as non-formal
and provisional. Each candidate displays its loop lifecycle, complete-round
count, safe public pause reason, best committed round, and scientific
assessment or revision warnings. It publishes
`<project-dir>/ideas/<run-id>/partial-ideas.pdf` and
`<project-dir>/partial-ideas.pdf`; it never substitutes for
`ranked-ideas.pdf`.

The formal report must start with `# Ideas`, then render the persisted
`Global Scientific Assessment (Advisory)` when available, then
`Abbreviations:` and a blank-line-separated abbreviation line in the form
`IR=intent relevance,
N=novelty, CN=confidence of novelty, SV=scientific value, PL=planning,
WD=well-definedness, SI=simplicity, GE=generality, T=total.` List each ranked idea in the same form used by
`round_marks_by_idea.md`: a loop-id heading, the selected title, and the
compact round marks table with columns `Round`, `IR`, `N`, `CN`, `SV`, `PL`,
`WD`, `SI`, `GE`, and `T`. The report must then include `# Appendix: Idea
Details` with one subsection per ranked idea. Each subsection lists all
referee marks from every round in that idea loop, the selected reviewer's
scientific-taste comparison and simpler same-direction alternative when
available, a focused novelty audit with evidence checked, tool queries, and
unresolved reviewer limitations, and only the selected proposer handoff text:
title, idea summary, and calculation plan. Scientific taste is a soft ranking
and revision preference; it does not independently remove or hide a candidate.
The audit is explicitly non-exhaustive.
Preserve citation-neighborhood evidence and exact ARC command records verbatim
inside that existing focused novelty audit; do not create a separate evidence
ledger or use the citation audit to alter scores, ranks, or visibility.
Render the handoff text as normal Markdown paragraphs, not a fenced code block.
Follow `rules/math_typeset.md` for math and TeX snippets. Use PDF-friendly
wrapping for long titles and proposer text; avoid wide tables with long prose.

For cross-domain runs, use the abbreviations and score columns declared by the
selected cross-domain marking scheme. List all trace-verified candidates in
formal ranking order and preserve transfer-quality concerns and concrete repair
advice without changing score, rank, or visibility.

For new single-domain runs, formal ranking likewise keeps every trace-verified
candidate visible. Preserve mathematical-definition and feasibility problems
as assessment and revision feedback; distinguish nucleus-breaking failures
from errors repairable in another round. No-assessment single-domain variants
remain visibly marked as using the `no_assessment` policy; do not infer an
alternate artifact layout.

Step 2: Attempt both visible PDFs from Step 1 before claiming PDF delivery. On
rendering failure, print `WARNING:` with the exact error and preserve the
hidden Markdown source and durable run for retry. PDF availability does not
change ranking, candidate visibility, scientific status, or eligibility for a
downstream workflow that the caller already requested.

Do not invent rankings or novelty claims. Use only public committed proposals,
review envelopes, and reviewer `payload` values returned through the
proposer-reviewer projection.

### Phase 4: Select Next Action

Step 1: Print the top three ranked ideas on screen.

Step 2: Stop after printing the top three ideas unless the caller explicitly
requested planning or calculation as part of the original request. In
particular, `auto` does not authorize a move to calculation outside that scope.

If the caller explicitly requested planning or calculation after idea
generation, proceed with ranked idea #1 in `auto` mode without asking. In
`interactive` mode, pause after the top three and use
the host's selection/menu tool, following `rules/interaction.md`, with these
option labels exactly:

- `Proceed with ranked idea #1 (Recommended)`
- `Proceed with ranked idea #2`
- `Proceed with ranked idea #3`

If no selection/menu tool is available, use the typed fallback from
`rules/interaction.md` with the same three options.

If no downstream workflow was requested, stop after printing the top three in
either mode; do not create a needless confirmation checkpoint.
