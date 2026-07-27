# Calculate Workflow

Use this workflow after `plan.md` writes `<project-dir>/.arc/calculate/<run-id>/work-note.md`.
Execute only steps marked ready in `Detailed Steps Ready To Calculate`.
Do not write a separate calculation report; the rendered work-note PDF is the human-readable result.

`calculate.md owns calculation execution` and the `current-step result-status`. It does not change ready-step boundaries, does not change rough steps, and does not change future plan structure; it does not own note parsing. When a different workflow owns the needed change, refer to it.

Heavy Workload Rule: This workflow can be long; heavy workload and many claims/equations are expected runtime facts. Workload size is not a stop condition. The agent must not skip mandatory phases or shorten requested coverage because work is heavy. Continue until requested coverage is complete, a concrete workflow stop condition applies, or the user explicitly stops the workflow.

## Phase 1: Prepare Runtime

Runtime artifacts:

```text
<project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json
<project-dir>/.arc/calculate/<run-id>/execute/<calculate-run-id>/config.json
<project-dir>/.arc/calculate/<run-id>/execute/<calculate-run-id>/state.json
```

The first file is the workflow input. Under one project-local lease, the runner binds the run ID and result to the normalized config semantic key. A conflicting binding is invalid; this guard is not a second durable workflow. Do not discover proposal, review, transcript, session, task, or group files by walking the directory.

Copy `workflows/json/calculate.config.template.json` to:

```text
<project-dir>/.arc/calculate/<run-id>/execute/calculate.config.json
```

Replace `<calculate-run-id>`, `<project-dir>`, `<run-id>`, and
`<skill-workflow-json-dir>`. Use `skill_dir` from context as `<skill-dir>` in commands below. The calculation configuration always uses exactly two calculators and a finite `"max_recalculations"`; calculator count is not a workflow choice.
Prompts are part of the durable semantic input and are always retained by the owned proposer-reviewer batch; there is no prompt-omission option.
The default template uses high reasoning effort and medium verbosity because these tasks are mathematical derivations, not lightweight summaries. Lower effort only for cheap exploratory runs.

The runner reads worker prompt/schema templates from `workflows/json/calculate-proposer.template.json`, `workflows/json/calculate-reviewer.template.json`, and `workflows/json/calculate-reviewer-output.schema.json`.

Keep the retry budget finite. A retry always starts two fresh, independent calculators; neither receives the other calculator's answer, a prior answer, or a reviewer-only reference. Both receive the same answer-free, method-focused instruction from the referee. Per-calculator feedback remains audit-only and is not reused. A retry needs a concrete new hypothesis, algorithm, or recovery path; do not use an unbounded retry loop. On the final allowed attempt the referee must choose `replan` or `pause_for_human`, not `retry`; the runner rejects a final `retry` as an invalid action contract.
Each ready step and attempt creates one deterministic, independent public `arc_proposer_reviewer.BatchRequest`: one loop, one committed round, exactly two active calculators, and one referee. Attempts do not reuse a private workflow session or artifact layout. The runner executes that request through `arc-jobs` and `arc-proposer-reviewer`, then reads proposal and review JSON only from the public committed round. A returned attempt records its public batch run ID and loop ID; use those identities rather than constructing artifact paths.

## Phase 2: Build Step Packets

For each current ready step, add one config step with the ready-step free-text calculation prompt, relevant work-note notation/axioms/accepted results/current ready step, and clean proposer-facing source context in `allowed_context`. The prompt must state the quantity, required representation in named quantities, conventions/regime/approximation order, and completion/agreement standard. Do not create a structured parsed target contract or put an exact target formula in the prompt.
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

The runner places this claim in referee instructions only, never puts it in calculator context, and redacts referee feedback before a blind retry. Blind calculators are instructed not to seek or use the reviewer-only claim; this is a scientific independence rule, not a host-capability sandbox. A source or reference mismatch is an explicitly untrusted remark. It may coexist with a trusted jointly derived result when the referee judges the result valid; an ambiguity that affects validity remains untrusted.

For a new derivation after a check, place the permitted source evidence and
its provenance in `allowed_context` before starting the batch:

```json
"allowed_context": {
  "accepted_sources": [{"canonical_id": "...", "excerpt": "...", "provenance": "..."}]
}
```

Use ARC paper tools or web research when more document evidence is needed. Workers may use available research tools and should record actual sources and results. External sources may guide methods, but any used identity or intermediate result must be derived or already accepted in the work note. Map all notation back to work-note conventions.

## Phase 3: Run Calculation

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

## Phase 4: Referee Trust Decision

The referee owns scientific validity and semantic equivalence. It evaluates two independent calculator results against the same free-text prompt, explaining validity, comparison, conventions, rewrites, identities, scope, and approximation order. Its report records calculator assessments, comparison reasoning, `trusted_results`, explicitly untrusted `remarks`, and one `workflow_action`: `continue`, `retry`, `replan`, or `pause_for_human`.

Only a result supported by both actual calculators and trusted by the referee may enter `trusted_results` or any accepted result. A one-calculator result, a remark, a source mismatch, or an unresolved alternative never becomes trusted or an allowed premise. The main agent audits report completeness and provenance (two committed calculator identities, a non-empty comparison, result support, and artifact integrity); it does not judge formula equivalence or promote a one-calculator result or remark.

SymPy, Wolfram, explicit algebra, analytic limits, and controlled numerical checks are optional scientific tools, not programmatic acceptance gates. Limits and numerics can discriminate between competing results, but are not automatic proof.

For a correctable same-step error, retry both calculators fresh within the finite budget. If the referee identifies a common trusted part, preserve that part and return the unresolved part to `plan.md`. `plan.md` may repeatedly replan into strictly smaller unresolved targets while each iteration makes scientific progress. When the target is atomic, run one dedicated adjudication round using an applicable analytic limit and/or controlled numerical check. If the referee still cannot resolve it, ask one precise human question showing the competing formulas and the evidence needed to decide.

If both calculators support and the referee trusts a specific equation or rule
as an agent-added foundation, do not pause for a human expert solely for that
promotion. Treat it as a nonhuman planning revision: record the exact equation
or rule, scope, batch run ID, loop ID, committed round, verified public refs,
and reason in a planning request, then return to `plan.md`. When `plan.md` adds
it, mark it with the red `[foundation added by agent]` marker described below.
Source target formulas, unresolved conventions, and broad unsupported rules
remain validation-only, accepted-derived, untrusted, or subject to a precise
human question.

When pausing for a human expert, do not merely say that the workflow paused.
Write and ask one concrete question under the literal label `Human expert
question:`. It must name the atomic unresolved target, competing formulas or
claims, available evidence, and the answer needed to decide. For important
equations, display the equation body or decision-critical subequation directly
in the work note and user-facing question. If long, show the minimal formula
fragment plus source equation id and anchor. Record the same question in `Open
Questions` or `Calculation Status`.

## Phase 5: Update Work Note

After each accepted step or coherent calculation chunk is written back and
exported, pause in `interactive` mode before starting the next block. This is a
general milestone; do not pause after each underlying tool call.

For a referee-trusted result, update the work note and remove its accepted step
from the executable backlog:

- only `trusted_results` go to `## Accepted Derived Results`
- every referee `remark` goes to `## Calculation Remarks — Not Trusted Results`
  with an explicit `untrusted` label; remarks never become premises
- remove the accepted step block from `## Detailed Steps Ready To Calculate`
- use main prose for the physics argument
- keep compact trace in `## Calculation Status`, `## Revision History`, and `## Journal`: step ID, trusted or untrusted status, attempt, referee action, both calculator IDs, batch run ID, loop ID, committed round, and verified public ref digests
- no `status: accepted` step block may remain under `## Detailed Steps Ready To Calculate`
- follow `rules/math_typeset.md` math/TeX hygiene

For an agent-added foundation, use this PDF-oriented Markdown marker template.
It is shown as code here only; in work notes paste the raw LaTeX directly in
prose, not inside Markdown code spans or fenced code blocks. If the work note
already has a YAML header, merge these `header-includes`; do not create a second
YAML header.
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
Do not use custom no-argument marker macros such as `\arcsourceissue` or
`\archumanresolved`; Pandoc may strip them from inline prose.

When the referee and the recorded evidence actually confirm a source issue,
put `[confirmed source issue]` beside the source-disagreement statement. When a
specific human expert answer is the reason otherwise-untrusted content becomes
accepted, put `[human-resolved]` beside that accepted content. In each case,
color only the literal marker background, not the surrounding prose or
equations. These are provenance annotations, not runner statuses: no source
mismatch classification or marker automatically pauses the workflow, changes
trust, or promotes a result.

For an agent-added foundation, put the literal marker `[foundation added by agent]` beside the foundation equation or rule, and only color that marker's background dark red with white text. The marker means both calculators supported it and the referee trusted it without a human pause; it does not mean the manuscript source was correct.

If the referee action is `retry`, retain the current step and run two fresh
calculators. If it is `replan`, record the trusted common part, if any, and
write the unresolved remainder and all remarks to a planning request. If it is
`pause_for_human`, mark the current step blocked and ask the exact same `Human
expert question:` in the user-facing response before ending the turn, including
the displayed equation body or formula fragment required by the question.

A human expert may resolve a precise atomic question and thereby unblock the
workflow; this is not by itself a completion condition. After recording the
answer, its provenance, and `[human-resolved]` beside any content accepted
because of that answer, continue unless the user explicitly asks to pause or
stop, a new `Human expert question:` is outstanding, a tool/runtime blocker
prevents progress, or `interactive` mode requires confirmation:

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

If the referee action is `replan`, a trusted result may become a candidate
reusable result, or an equation/rule may become an agent-added foundation, do
not edit ready-step boundaries, rough steps, or future plan structure from
`calculate.md`.

Write `<project-dir>/.arc/calculate/<run-id>/planning-request.md` with:

- current step ID/status, batch run ID, loop ID, committed round, verified public refs, both calculator positions, referee comparison and action, trusted common results, explicitly untrusted remarks, the strictly smaller unresolved target, and requested `plan.md` action
- for agent-added foundation requests: exact equation or rule, validity scope, why it should live in `## Axioms And Starting Points`, and confirmation that both calculators support it and the referee trusts it

Then return to `plan.md`. Use the same handoff when blocked refinement needs splitting, limits, projections, different source context, or changed future premises. When the issue came from note parsing or claim extraction, refer to the owning workflow instead of changing it here.
