# Core Refactor Downstream Breakage Inventory

This inventory records consumers intentionally left for later strangler
migration after `arc-jobs`, `arc-llm`, and `arc-proposer-reviewer` 1.0.1 became
stable. It is not a compatibility backlog for the new core: the removed APIs
must not be reintroduced as shims.

The package versions were unified at 1.0.1, but the implementations listed
below have not yet been migrated.

## arc-paper

- `arc_paper/batch/runner.py`, `reference_inference.py`, `service.py`, and
  `summary/providers/{claude_cli,codex_cli,prompt}.py` import the removed
  `arc_llm.runner` (`run_json`, `resolve_llm_config`).
- `arc_paper/broker_jobs.py` imports removed `JobManager`, `JobPaths`, raw
  JSON/progress/lock helpers and `arc_jobs.jobs.restored_environment`; it also
  imports removed LLM budget and call-checkpoint APIs.
- `arc_paper/host.py` imports the removed host-selection facade.
- `arc_paper/reference_inference.py` and
  `summary/providers/pipeline.py` import removed call-record fields/helpers.
- `arc_paper/summary/model.py` imports removed model resolution.
- `arc_paper/summary/providers/base.py` imports the removed
  `LLMWorkerError`.
- `arc_paper/summary/providers/{claude_cli,codex_cli,select}.py` import old
  provider implementations/registry helpers.
- `arc_paper/worker_cli.py` imports the removed paper-access policy.

## arc-domain

- `foundation.py`, `network.py`, and `summary.py` import removed `run_json`
  and call-record helpers.
- `llm_safety.py` imports removed `LLMAbortScope` and
  `failure_disposition`.
- `service.py` imports the removed `LLMNeedsLLM`.

## arc-typeset

- `translate.py` imports removed `arc_llm.runner.run_json`.

## arc-companion

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

`arc-mcp` is the deliberately retained transitional MCP package and is not part
of the new core:

- `cli.py` and `jobs.py` import removed `JobManager`/`JobCancelled` and cache
  topology.
- `server.py` imports removed jobs CLI command generation, host/provider
  diagnostics and old LLM config.
- `worker.py` imports the removed detached jobs worker.

The package will be retired only in the later migration stage, together with
its plugin and entrypoints.

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
