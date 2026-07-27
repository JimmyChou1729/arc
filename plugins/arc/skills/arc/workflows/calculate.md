# Calculate Workflow

Use this workflow after `plan.md` writes `<project-dir>/.arc/calculate/<run-id>/work-note.md`.
Execute only steps marked ready in `Detailed Steps Ready To Calculate`.
Do not write a separate calculation report; the rendered work-note PDF is the human-readable result.

`calculate.md owns consensus execution` and the `current-step result-status`. It
does not change ready-step boundaries, does not change rough steps, and
does not change future plan structure. Calculate does not own note parsing. When
a different workflow owns the needed change, refer to the owning workflow.

Heavy Workload Rule: This workflow can be long. Heavy workload and many
claims/equations are expected runtime facts; workload size is not a stop condition.
The agent must not skip mandatory phases or shorten requested coverage because work is heavy.
Continue until requested coverage is complete, a concrete workflow stop condition applies, or the user explicitly stops the workflow.

## Phase 1: Prepare Runtime

Runtime artifacts:

```text
<project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json
<project-dir>/.arc/calculate/<run-id>/execute/<calculate-run-id>/config.json
<project-dir>/.arc/calculate/<run-id>/execute/<calculate-run-id>/state.json
```

The first file is the workflow input. Under one project-local lease, the runner
binds the run ID and result to the normalized config semantic key. A conflicting
binding is invalid; this guard is not a second durable workflow. Do not discover
proposal, review, transcript, session, task, or group files by walking the directory.

Copy `workflows/json/calculate.config.template.json` to:

```text
<project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json
```

Replace `<calculate-run-id>`, `<project-dir>`, `<run-id>`, and
`<skill-workflow-json-dir>`. Use `skill_dir` from context as `<skill-dir>` in commands below. Keep `"proposer_count": 2` and `"max_recalculations": 1`.
Prompts are part of the durable semantic input and are always retained by the
owned proposer-reviewer batch; there is no prompt-omission option.
The default template uses high reasoning effort and medium verbosity because these tasks are mathematical derivations, not lightweight summaries. Lower effort only for cheap exploratory runs.

The runner reads worker prompt/schema templates from `workflows/json/calculate-proposer.template.json`, `workflows/json/calculate-reviewer.template.json`, and `workflows/json/calculate-reviewer-output.schema.json`.

`"max_recalculations": 1` means 2 total attempts: 1 initial attempt + 1 recalculation.
Keep this finite default. The main agent may choose a higher finite value without
asking only when it records a concrete new hypothesis, algorithm, or recovery
path that makes the additional attempt scientifically distinct. Do not repeat
the same strategy or use an unbounded retry loop.
Each ready step and attempt creates one deterministic, independent public `arc_proposer_reviewer.BatchRequest`: one loop, one committed round, the active proposers, and one reviewer. Attempts do not reuse a private workflow session or artifact layout. The runner executes that request through `arc-jobs` and `arc-proposer-reviewer`, then reads proposal and review JSON only from the public committed round. A returned attempt records its public batch run ID and loop ID; use those identities rather than constructing artifact paths.
For retryable proposer disagreement statuses, use the recalculation budget before pausing for human input. A `two_agree` decision locks the two accepted proposer outputs and runs only the one selected proposer again. Other retryable outcomes restart the active proposer set within the remaining budget.

## Phase 2: Build Step Packets

For each current ready step, add one config step with the current step prompt,
quantity contract, relevant work-note notation/axioms/accepted results/current
ready step, and clean proposer-facing source context in `allowed_context`.
Do not expose reviewer-only targets, target equations, or later note text to proposers.
Set `kind` explicitly:

- `new_derivation` for deriving a target without assuming its result;
- `check_known_result` for independently checking a stated result;
- `formal_setup` for constructing a formal object or controlled setup whose
  downstream reduction remains.

For a blind reference check, include `reviewer_reference_claim` only in the
`check_known_result` step object:

```json
"reviewer_reference_claim": {"claim_id": "...", "statement": "..."}
```

The runner places this claim in reviewer instructions only, never puts it in
proposer context, and redacts reviewer feedback before a blind retry. Blind
proposers are instructed not to seek or use the reviewer-only claim; this is a
scientific independence rule, not a host-capability sandbox. If blind proposers
agree with each other but not with the reviewer reference, record
`reference_disagrees`; use remaining recalculation budget with a generic
independent-recomputation instruction before pausing for a human decision.

For a new derivation after a check, place the permitted source evidence and
its provenance in `allowed_context` before starting the batch:

```json
"allowed_context": {
  "accepted_sources": [{"canonical_id": "...", "excerpt": "...", "provenance": "..."}]
}
```

Use ARC paper tools or web research when more document evidence is needed.
Workers may use available research tools and should record actual sources and
results. External sources may guide methods, but any used identity or
intermediate result must be derived or already accepted in the work note. Map
all notation back to work-note conventions.

## Phase 3: Run Consensus

Run:

```bash
python3 <skill-dir>/scripts/run-calculate.py \
  --config <project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json \
  --host-authority <host-authority>
```

Set `<host-authority>` to `unrestricted` only when the host explicitly reports
unrestricted permissions; otherwise set it to `unknown` and reuse that value
when resuming the same calculation run. Under `restricted` or `unknown`, a model
host request becomes a durable manual pause without an explicitly supplied
broker; inspect the typed pause and resume the same run with its required input.
ARC does not assume a production universal broker.

The command exits `0` for `completed`, `dry_run`, `blocked_for_user`, and
`blocked_for_revision`. A blocked result is a normal nonterminal workflow
handoff: read its `blocked_output`, preserve the saved state, and follow the
human or planning action instead of treating it as success or failure. The
command exits `1` for a `failed` result and `2` for command usage or invalid
configuration.

Inspect the returned JSON and saved calculation state. Large or slow runs are
runtime facts, not workflow blocks. Use the owning package's status command or
the host's background-command facility instead of frequent manual polling.

When a completed attempt needs durable inspection, use only the public proposer-reviewer inspection surface with the returned batch run ID:

```bash
arc-proposer-reviewer inspect --run-root <attempt-batch-run-root> --run-id <batch-run-id>
arc-proposer-reviewer trace --run-root <attempt-batch-run-root> --run-id <batch-run-id>
arc-proposer-reviewer show-round --run-root <attempt-batch-run-root> \
  --run-id <batch-run-id> --loop-id <loop-id> --round 1
```

`trace` exposes only verified committed refs and summaries. `show-round` is the only public operation here that expands proposal and review bodies. Never infer state from unpublished half-round artifacts; they are intentionally invisible.

## Phase 4: Review Acceptance

Acceptance depends on reviewer judgment. SymPy, Wolfram, explicit algebra, and numerical checks are optional tools, not mandatory gates. Accept only if the target quantity agrees in the declared regime and approximation order.

The reviewer must explain the comparison, conventions, rewrites, and identities used to relate expressions. Special limits are sanity checks, not proof of full agreement unless the target itself is a limit, asymptotic result, or leading-order claim.

The main agent audits the reviewer report before updating the work note. Reject weak evidence such as formatting agreement, visual similarity, or agreement in an undeclared special limit. Depending on the failure, retry, split, pause for the expert question, or write a planning request.

If proposers, reviewer, and the main agent all agree that a specific equation or rule should be added to `## Axioms And Starting Points`, do not pause for a human expert solely for that promotion. Treat it as a nonhuman planning revision: record the exact equation or rule, scope, batch run ID, loop ID, committed round, verified public refs, and reason in a planning request, then return to `plan.md`. When `plan.md` adds it, mark it with the red `[foundation added by agent]` marker described below. Source target formulas, unresolved conventions, and broad unsupported rules still require the ordinary validation-only, accepted-derived, blocked, or human-question path.

If an accepted derivation contradicts a source or reviewer-only reference claim, classify each independent discrepancy before updating the work note. A step may have zero or more `source_discrepancies`; do not merge unrelated equations or claims into one aggregate status:

- `confirmed_source_error`: blind proposers agree, the reviewer agrees, the
  main agent agrees with the reviewer, the derivation uses only accepted
  premises, the mismatch is not convention-dependent, and the reviewer says no
  human convention choice is needed.
- `likely_source_error`: the derivation probably identifies a source problem,
  but one of the confirmation requirements is missing or weak.
- `ambiguous_convention`: the mismatch may be due to convention, normalization,
  notation, source mapping, or interpretation.

Before a step can become an accepted premise, every source-discrepancy item must fall into exactly one of the below two cases:

1. It is classified as `confirmed_source_error`; mark the source-disagreement
   statement with `[confirmed source issue]`. In reviewer JSON, keep that item's
   `decision_question` as an empty string.
2. It is classified as `likely_source_error` or `ambiguous_convention`; pause
   and ask a `Human expert question:` before accepting the item. After the
   human answer resolves the item, mark the accepted content with
   `[human-resolved]`. If the human answer confirms a source issue, also mark the
   source-disagreement statement with `[confirmed source issue]`.

When pausing for a human expert, do not merely say that the workflow paused. Write and ask one concrete question under the literal label `Human expert question:`. If multiple source-discrepancy items remain unresolved in the same step, enumerate each item in the same question. Each item must name the step, unresolved equation or claim, competing options, and answer needed to proceed. For important equations, do not cite equation ids alone: display the equation body or decision-critical subequation directly in the work note and user-facing question. If long, show the minimal formula fragment plus source equation id and anchor.
Record the same question in `Open Questions` or `Calculation Status`.

## Phase 5: Update Work Note

After each accepted step or coherent calculation chunk is written back and
exported, pause in `interactive` mode before starting the next block. This is a
general milestone; do not pause after each underlying tool call.

For an accepted step, update the work note and remove it from the executable backlog:

- accepted step result goes to `## Accepted Derived Results`
- remove the accepted step block from `## Detailed Steps Ready To Calculate`
- use main prose for the physics argument
- keep compact trace in `## Calculation Status`, `## Revision History`, and `## Journal`: step ID, accepted status, attempt, reviewer status, source discrepancy status, batch run ID, loop ID, committed round, and verified public ref digests
- no `status: accepted` step block may remain under `## Detailed Steps Ready To Calculate`
- follow `rules/math_typeset.md` math/TeX hygiene

For PDF-oriented Markdown marker backgrounds, use this exact template. It is shown as code here only; in work notes paste the raw LaTeX directly in prose, not inside Markdown code spans or fenced code blocks. If the work note already has a YAML header, merge these `header-includes`; do not create a second YAML header.
```yaml
---
header-includes:
  - \usepackage{xcolor}
  - \definecolor{arcsourceissue}{HTML}{8B0000}
  - \definecolor{archumanresolved}{HTML}{003F8C}
---
```
```tex
\colorbox{arcsourceissue}{\textcolor{white}{[confirmed source issue]}}
\colorbox{arcsourceissue}{\textcolor{white}{[foundation added by agent]}}
\colorbox{archumanresolved}{\textcolor{white}{[human-resolved]}}
```
Do not use custom no-argument marker macros such as `\arcsourceissue` or `\archumanresolved`; Pandoc may strip them from inline prose.

For `confirmed_source_error`, put the literal marker `[confirmed source issue]` beside the source-disagreement statement, and only color that marker's background dark red with white text. Do not color the surrounding prose. Do not color the surrounding equations. If color is stripped or unavailable, the marker remains authoritative. Do not use the red marker for likely or convention-dependent source mismatches until the human expert question is answered and confirms a source issue.

For an agent-added foundation, put the literal marker `[foundation added by agent]` beside the foundation equation or rule, and only color that marker's background dark red with white text. The marker means proposer/reviewer/main-agent consensus promoted it without a human pause; it does not mean the manuscript source was correct.

If the step is blocked, mark the current ready step blocked and record the disagreement, proposer positions, reviewer judgment, expert question, and proposed next action. If the block needs human input, ask the exact same `Human expert question:` in the user-facing response before ending the turn, including any displayed equation body or formula fragment required by the question.
Limits diagnose; they are not proof.

Any accepted work-note content whose acceptance depends on a specific human expert answer resolving an unresolved scientific acceptance question is human-resolved content. This includes a blocked step the expert resolves, a source convention the expert chooses, a likely source error the expert confirms, or a main-agent verification accepted only because the expert allowed that standard. It excludes ordinary user task instructions, source excerpts, or constraints.
Add the literal marker `[human-resolved]` beside the accepted content, and only color that `human-resolved` marker's background dark blue with white text. Do not color the surrounding prose. Do not color the surrounding equations. If color is stripped or unavailable, the marker remains authoritative. Do not surround color commands with backticks.

A human expert later resolves a block and thereby unblocks the workflow; this is not by itself a completion condition. After recording the human-resolved result, continue unless the user explicitly asks to pause or stop, a new `Human expert question:` is outstanding, a tool/runtime blocker prevents progress, or `interactive` mode requires confirmation:

- If another ready detailed step exists, continue with the next ready detailed step using this workflow.
- If no ready detailed step exists but rough/pending coverage remains from the original request, write a planning request and return to `plan.md` to promote the next coherent chunk.
- End the turn only when requested coverage is complete or one of the stop conditions above applies.

When a result may help later steps, record it only as a candidate reusable
result. Promotion to an accepted premise belongs to `plan.md`.

Write an immutable next work-note version at
`<project-dir>/.arc/calculate/<run-id>/work-notes/work-note-vNNN.md`, then
mirror it to `<project-dir>/.arc/calculate/<run-id>/work-note.md`. After
writing the hidden current work note, follow
`manuals/arc-jobs.md` Markdown Report Export for
`<project-dir>/.arc/calculate/<run-id>/work-note.md` and atomically replace
`<project-dir>/work-note.pdf`. If rendering fails, record a `WARNING:` with the
exact blocker and preserve calculation state, but do not claim PDF delivery or
prevent an otherwise-authorized handoff from the verified hidden work note.

## Phase 6: Planning Handoff

If proposers, reviewer, or the main agent agree that plan content should change, that a candidate reusable result should become a future premise, or that an equation/rule should become an agent-added foundation, do not edit ready-step boundaries, rough steps, or future plan structure from `calculate.md`.

Write `<project-dir>/.arc/calculate/<run-id>/planning-request.md` with:

- current step ID/status, batch run ID, loop ID, committed round, verified public refs, evidence, proposer positions, reviewer judgment, and requested `plan.md` action
- for agent-added foundation requests: exact equation or rule, validity scope, why it should live in `## Axioms And Starting Points`, and confirmation that proposers, reviewer, and main agent agree

Then return to `plan.md`. Use the same handoff when blocked refinement needs splitting, limits, projections, different source context, or changed future premises. When the issue came from note parsing or claim extraction, refer to the owning workflow instead of changing it here.
