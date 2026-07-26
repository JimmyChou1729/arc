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
transfer profiles, reviewer assessment, and qualification gates. Cross-domain
cards and source/target roles use `field_id`. A missing, unsupported, or invalid
manifest must be regenerated before any ideas work.

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
five distinct exploration profiles. If a different loop count is required,
set top-level `exploration_profiles` to the same number of profile objects so
the runner never creates duplicate loops that differ only by ID.

### Phase 2: Run Ideas

Step 1: Run:

```bash
python3 <skill-dir>/scripts/run-ideas.py \
  --config <project-dir>/.arc/ideas/<run-id>.config.json \
  --host-authority <host-authority> \
  --json
```

LLM calls have no absolute runtime limit and stop after 30 minutes with no
substantive provider output. The foreground runner streams batch, loop, round,
and worker boundary progress JSON to stderr. Keep its terminal session active;
theoretical research can take a long time, so do not interrupt merely because
the run is slow.

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
snapshots show recurring errors or a provider that is no longer making useful
progress:

```bash
<skill-dir>/scripts/arc-runtime arc-proposer-reviewer stop \
  --run-root <project-dir>/.arc/ideas \
  --run-id <run-id> \
  --reason "<specific observed reason>"
```

Use stop cautiously and record the concrete reason. `SIGINT` and `SIGTERM`
remain available for the foreground process. A stop pauses the current attempt
for same-run resume.

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
formal ranking. For loop concurrency and durable pause/resume behavior, see
`manuals/arc-proposer-reviewer.md`.

### Research Tools

ARC and web search are complementary research surfaces. Workers may use the
web, ARC paper tools, and the shared ARC paper cache when those capabilities
are available and useful. They should use focused checks rather than exhaustive
searching, inspect the strongest candidates first, and record each actual
source or query with a short result. If a host or provider cannot supply a
capability, state the limitation once and continue from the available evidence.

Set `<host-authority>` to `unrestricted` only when the host explicitly reports
unrestricted permissions; otherwise set it to `unknown` and reuse that value
when resuming the same run. Restricted hosts use the generic host broker; this workflow does not define a
paper-operation allowlist, a tool ledger, or a host-specific fallback. The
runtime holder supplies this explicit attestation and the LLM service selects
the corresponding safe mode.

Final ranked ideas must come from `run-ideas.py`'s public committed batch data
and the read-only ranking helper, not ad-hoc agent judgment. In cross-domain mode,
only candidates marked as genuine transfers with a substantial target-domain
contribution and a feasible first calculation are eligible for the formal
ranking. The source domain may contribute a mature method or mechanism without
itself receiving a new result.

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
Feasibility is a qualification gate, while problem importance is scored
strongly rather than used as a binary gate. Do not promote a convenient but
low-value exercise, or an important problem without ready inputs and a bounded
first calculation.

### Phase 3: Inspect Artifacts

Report the durable repository root and run ID, not internal run-layout paths:

```text
run root: <project-dir>/.arc/ideas
run ID: <run-id>
request artifact: proposer-reviewer/request
```

Step 1: Use the public observation APIs after the run completes. `inspect_batch`
may show a lifecycle for any batch state; the strict trace exposes only complete
committed rounds. The ranking helper selects the best qualified committed round
for each succeeded loop, rather than assuming the final round is best. It emits
logical artifact IDs and content digests, never physical proposal or review
paths.

The JSON ranking contract is `arc.ideas.selected_rounds.v5`. Read its
scientific `status` separately from `durable_lifecycle`: a durable batch may
finish successfully while the scientific status is `degraded` because one or
more loops failed. The current contract has no `run_lifecycle` alias.

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

The report must start with `# Ideas`, then `Abbreviations:`, then a
blank-line-separated abbreviation line in the form `IR=intent relevance,
N=novelty, CN=confidence of novelty, SV=scientific value, PL=planning,
WD=well-definedness, T=total.` List each ranked idea in the same form used by
`round_marks_by_idea.md`: a loop-id heading, the selected title, and the
compact round marks table with columns `Round`, `IR`, `N`, `CN`, `SV`, `PL`,
`WD`, and `T`. The report must then include `# Appendix: Idea Details` with one subsection per
ranked idea. Each subsection lists all referee marks from every round in that
idea loop, a focused novelty audit with evidence checked, tool queries, and
unresolved reviewer limitations, and only the selected proposer handoff text:
title, idea summary, and calculation plan. The audit is explicitly
non-exhaustive. Render the handoff text as normal Markdown paragraphs, not a
fenced code block. Follow `rules/math_typeset.md` for math and TeX snippets.
Use PDF-friendly wrapping for long titles and proposer text; avoid wide tables
with long prose.

For cross-domain runs, use the abbreviations and score columns declared by the
selected cross-domain marking scheme. List qualified candidates first in
formal ranking order, never fill the top three with an unqualified candidate,
and add an unqualified appendix with explicit reasons. Print any
insufficient-qualified-candidate `WARNING:` messages.

For new single-domain runs, formal ranking likewise contains only candidates
that pass the mathematical-definition and feasibility gate. Do not pad the top
three with infeasible candidates. Add explicit failures to the unqualified
appendix and print any insufficient-qualified-candidate `WARNING:` messages.
No-assessment single-domain variants remain visibly marked as using the
`no_assessment` policy; do not infer an alternate artifact layout.

Step 2: Require both visible PDFs from Step 1 before claiming ideas delivery.
On rendering failure, print `WARNING:` with the exact error and preserve the
hidden Markdown source and durable run for retry. Do not proceed to a
downstream workflow or claim delivery until the PDFs exist.

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
