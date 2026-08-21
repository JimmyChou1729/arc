# arc-domain

`arc-domain` builds a reusable research-domain package from a seed paper and
scientific intent. It owns foundation and domain-paper selection, citation
graphs, evidence and paper packs, domain summaries, HTML rendering, and
published domain generations. It calls `arc-paper` for paper data and
`ac-llm` for model work. Project-level package relationships and manifest
publication remain in the ARC Skill, where relationship evidence is advisory
and does not determine a scientific route.

Durable builds accept only the closed `arc.domain_build_policy.v2` and
`arc.domain_build_request.v2` contracts. The default policy infers the
foundation from the seed and combines representative and recent citers;
callers may instead select fixed-seed and strict-window modes explicitly.
Exported summary packages accept the current `arc.domain_summary.v5`
contract.

## Running the CLI

An installed package provides the `arc-domain` console script. Check it first;
when working through the ARC Skill, use its portable runtime launcher. Inside
an ARC source checkout, the package virtual environment is a direct development
fallback:

```bash
arc-domain --help
<skill-dir>/scripts/arc-runtime arc-domain --help
packages/arc-paper/.venv/bin/arc-domain --help
```

Use the first available launcher consistently in later commands.

## Quick start

Build one domain with an explicit research intent:

```bash
arc-domain build "<seed-paper-id>" \
  --intent "<research-intent>" \
  --project-dir "<project-dir>" \
  --host-authority <host-authority>
```

Replace the seed, research-intent, project-directory, and authority
placeholders with values appropriate to the build. Durable state is stored in
`<project-dir>/.arc/domain`. Unless `--paper-cache-root` is provided, paper
data uses `ARC_PAPER_CACHE` when set, otherwise
`<launch-directory>/.arc/cache/arc-paper`; keep the launch directory stable
across related commands.

A completed published build returns the durable attempt ID at `run.id` and the
stable published domain ID at `data.domain.id`. Use the former for status,
resume, stop, and validation; use the latter for active-domain reads and
exports:

```bash
arc-domain get-summary --project-dir "<project-dir>" \
  --domain-id "<data.domain.id>"
arc-domain get-graph --project-dir "<project-dir>" \
  --domain-id "<data.domain.id>"
arc-domain materialize-export --project-dir "<project-dir>" \
  --domain-id "<data.domain.id>" --name evidence-pack \
  --output ./evidence-pack.json
```

The read commands return their documents at `data.summary` and `data.graph`.
Materialization verifies the active export, never overwrites an existing
output, and reports its path and digest at `data.export.output` and
`data.export.digest`. Use `arc-domain --help`, `arc-domain build --help`, and
`arc-domain materialize-export --help` for build policy, inspection, all five
public export names, and run controls.

`--host-authority` is an execution-only attestation for model calls. It
defaults to `unknown`; use `unrestricted` only when the invoking host has
explicitly granted those permissions. It is not part of the domain request or
published artifacts, so provide the same value again when resuming such a run.

A failed or paused build can be diagnosed and explicitly resumed with the same
run ID. `status` reports `can_resume`, `recovery_epoch`, and the stable
`working/` paths. A structurally valid domain-summary response that fails
package identity or evidence-coverage validation receives one deterministic
fresh model retry with bounded validation feedback. If that retry is also
machine-invalid, the build pauses with the retry candidate in `working/` and
both attempts retained as diagnostics; an agent may correct the candidate and
resume without another provider call. Deleting the candidate does not grant a
third automatic generation attempt. This retry is for unusable machine output,
not for revising a valid scientific judgment. Editing an upstream semantic
input or artifact is accepted with a warning; the agent is responsible for
deleting downstream working files that should be rebuilt. The previously
active domain generation stays published until the recovered build fully
validates.

## Tests

The package suite uses fake paper and model services by default:

```bash
python -m pytest packages/arc-domain/tests
```
