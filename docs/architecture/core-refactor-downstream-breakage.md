# Core Refactor Downstream Breakage Inventory

This inventory records consumers intentionally left for later migration after
the `arc-jobs`, `arc-llm`, and `arc-paper` core refactors. It is not a
compatibility backlog: removed APIs must not be reintroduced as shims.

The durable core includes `arc-jobs`, `arc-llm`, `arc-paper`, and the rebuilt
`arc-domain`. Sections below distinguish migrated surfaces from consumers that
are intentionally deferred.

## arc-paper

`arc-paper` itself is migrated. Its broker, worker wrapper, session/guard,
runtime context, private execution context, SQLite batch runner, summary
checkpoint/provider adapters, legacy cache control plane, duplicate parsers,
and arXiv TeX source provider were deleted. The supported core is the
content-addressed source repository, provider acquisition, unified parser and
reconciler, deterministic typed full-text/equation search, optional full-page
visual review, typed workflow handlers, and the filtered operation registry.

Downstream consumers must migrate from old result envelopes and paper-specific
worker commands to typed Python values, `arc.command_result.v1`, and
`arc-jobs` run controls. Old cache and checkpoint state remains in place but is
not read or migrated.

## arc-domain

`arc-domain` is rebuilt around the durable `arc.domain.build.v1` handler and
the closed build request/result contracts. It reads no old domain cache, result,
or durable state. The supported CLI is only `build`, `resume`, `status`,
`get-summary`, `get-graph`, `cancel`, and `validate`; legacy incremental and
`llm-*` commands have no compatibility shim.

The build is fixed to INSPIRE and the concrete `ArcPaperService`. It does not
provide a citation-provider abstraction, provider/query/search selector, or
refresh surface. Durable run artifacts are published to catalog-controlled
export generations; consumers must use those exported artifacts or the typed
domain result rather than old cache paths.

## arc-typeset

Package removed on 2026-07-24: reuse was limited to the now-removed arc-mcp
adapter and episodic CLI calls, and document-producing packages
(arc-companion, arc-domain) build their own integrated rendering/translation
pipelines, so the generic typesetting abstraction had no reuse potential.
Standalone Markdown-to-PDF is preserved as the canonical Pandoc/XeLaTeX
command in `plugins/arc/skills/arc/rules/math_typeset.md`; report translation
is handled natively by the agent. See
`local/core-refactor-implementation-2026-07-24/arc-typeset-removal.md`.

## arc-companion

`arc-companion` is explicitly out of scope for this migration. It still relies
on the previous service and cache topology and is not required to consume the
new domain result, catalog, or export layout in this release.

- `cli.py`, `paper_broker.py`, `pipeline.py`, and
  `translation_reference.py` depend on removed paper-access, evidence request,
  evidence journal/controller, and nested-shell APIs.
- `intent_guidance.py` depends on removed `EvidenceResponse`.
- `observability.py` depends on removed attempt-diagnostic sanitization.
- `pipeline.py` also depends on removed session manager, recovery context,
  budget, call-record, old runner, cancellation-chain, worker-error and
  provider submission-state APIs.
- `recovery_responses.py` depends on removed checkpoint promotion, candidate
  receipt, raw completion, schema-cache, usage and response-candidate APIs.
- `review_arbitration.py` and `review_reuse.py` depend on removed call-record
  stripping.
- `secure_io.py` imports the removed LLM-owned secure-I/O helpers.

## arc-mcp

ARC 0.9.4 was the final release line that shipped the legacy ARC-owned MCP
adapter. ARC 1.x is CLI-and-Skill only.

Package, plugin, launcher, entrypoint, runtime-profile wiring, and dependency
matrix row removed on 2026-07-24. The adapter depended on retired service,
cache, detached-worker, and result-envelope surfaces and had no independent
contract worth preserving. No compatibility shim or state migration is
provided. Consumers must use the supported package CLIs and the agent-facing
Skill workflows. Historical pre-v1 closure fixtures remain as audit records,
while generic host-inherited MCP warnings and worker no-host-tool instructions
remain safety policy rather than an ARC MCP compatibility surface.

## Skill workflow scripts

Skills remain the agent-facing layer, but these scripts still require their own
later migration:

- `calculate_runner.py` imports removed paper-access policy and the old
  proposer-reviewer artifacts/config/runner.
- `ideas_runner.py` imports removed evidence types, old proposer-reviewer
  runner/artifact helpers, and template materialization.
- `select-cross-domain-partner.py` imports removed `run_json`.
- `write-domain-manifest.py` imports removed `run_json`, `LLMAbortScope`, and
  `failure_disposition`.

Until migrated, these consumers are expected to fail import or focused tests.
The permanent dependency tests still prevent them from introducing reverse
edges into the new core.
