# arc-domain

`arc-domain` builds a reusable research-domain package from a seed paper and
scientific intent. It owns foundation and domain-paper selection, citation
graphs, evidence and paper packs, domain summaries, HTML rendering, and
published domain generations. It calls `arc-paper` for paper data and
`arc-llm` for model work. Project-level package relationships and manifest
publication remain in the ARC Skill, where relationship evidence is advisory
and does not determine a scientific route.

Durable builds accept only the closed `arc.domain_build_policy.v2` and
`arc.domain_build_request.v2` contracts. The default policy infers the
foundation from the seed and combines representative and recent citers;
callers may instead select fixed-seed and strict-window modes explicitly.
Exported summary packages accept the current `arc.domain_summary.v5`
contract.

## Quick start

Build one domain with an explicit research intent:

```bash
arc-domain build "<seed-paper-id>" \
  --intent "<research-intent>" \
  --project-dir "<project-dir>" \
  --host-authority <host-authority>
```

Replace both quoted placeholders with the seed identifier and the scientific
focus for the domain. Durable state is stored in
`<project-dir>/.arc/domain`; paper data uses the shared `arc-paper` cache
unless `--paper-cache-root` selects another explicit cache.
The result reports durable run and domain identities. Use `arc-domain --help`
and `arc-domain build --help` for build policy, inspection, published-artifact
queries, and run controls.

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
