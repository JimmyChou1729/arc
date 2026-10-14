# Host coordination

Use this manual when ARC returns `awaiting_host` or a typed pause containing
`details.code=awaiting_host` / `details.llm_code=awaiting_host`. Python exports
durable work and exits; the calling agent completes or delegates that work,
submits its response, and resumes the owning workflow. Never poll a blocking
model bridge, monkeypatch installed packages, or edit private state JSON.

## Phase 1: Check the execution environment

Confirm shell/Python, writable durable project storage, runtime installation,
and the tools available to the current agent. No plugin ZIP creates those
capabilities. Configure the neutral coordinator declaration only after checking
them. For example, with an actual independent-subagent facility:

```bash
export AC_LLM_HOST_COORDINATOR='{"coordinator_id":"my-agent","default_provider":"host","native_fallback":true,"fresh_context":true,"model_selection":false}'
```

Use `default_provider=codex`, `claude`, or `kimi` in their coding-agent hosts;
healthy native execution stays preferred. Only confirmed missing executables
may fall back before the first launch. Work, Cowork, and dot default to Host.
DSH retains its native bridge. Keep user-specified provider policy authoritative;
`native_fallback=false` enforces native execution. Never switch after a native
generation starts, after permission/authentication refusal, or to bypass a stop.

The declaration is an agent capability attestation, not a hidden Python SDK.
Do not set `fresh_context=true` merely because of the platform's name. Check
whether its tools can create independent contexts. For Codex, select a fresh
context rather than forking the coordinator's complete history; on other hosts,
use their equivalent tool. Discover the current tool names and parameters.
The generic Python packages do not call host-specific agent APIs.

Without explicit preferences, ARC leaves the model and reasoning effort unset;
the Host model resolution is `inherit` for every tier. The coordinator should
request the host defaults or inheritance using its available tools. Actual
subagent inheritance depends on those tools and is not guaranteed by Python.
`model_tier: "high"` does not set `reasoning_effort: "high"`. Calculate and Ideas
worker templates currently expose provider/model/tier, while the underlying
`ac-llm` request also supports explicit reasoning effort.
When the host supports selection, pass the requested model/effort to
its subagent interface. Otherwise, or when that model is unavailable, use the
host model under the configured `use_host_model` policy and record the actual
selection or null when unverified. Do not translate GPT model names into Claude
models as if they were equivalent. Usage and model identity remain unknown
unless the host provides them.

## Phase 2: Complete and submit pending work

Read the returned owning `run_root` and `run_id`. For calculate these are under
`steps[].resume`; relationships use the manifest's
`domain_relationships.awaiting`; Ideas uses `resume` for the batch and
`portfolio_assessment.resume` for the advisory child run.

```bash
<skill-dir>/scripts/arc-runtime ac-llm host-pending \
  --run-root <run-root> --run-id <run-id>
<skill-dir>/scripts/arc-runtime ac-llm host-export \
  --run-root <run-root> --run-id <run-id> --task-id <task-id> \
  --output-directory <project-dir>/.arc/host-exports/<task-id>
```

Read `task.json`, `host/control.json`, and the verified materialized inputs.
Read accepted session and host-turn history; historical files have separate
`history/session/` and `history/host/` locations. Use `work/` for scratch work.
Complete the exported `response.template.json` according to `response_schema`.
When the output contract is `ac.llm.host_turn.v1`, return the complete envelope,
including `state`, `result`, and `host_request`; a bare scientific result is
not sufficient. Respect the exported host authority and tool boundaries.

Calculate requires two independent calculator contexts, then a separate referee
context after both outputs are accepted. Give each calculator only its own
exported task and permitted inputs; do not share peer derivations or the
coordinator's reasoning. The referee receives both outputs through the original
workflow. Shared filesystem access is not OS isolation, and independent agents
need not be different models. If the host lacks independent contexts, report
the limitation instead of changing roles in one conversation.

For independent workers, the exported schema requires `actor.kind=subagent`,
a nonblank `actor.context_id`, and `isolation=fresh_context`. The template uses
`subagent` but leaves identity and isolation unconfirmed. Fill those fields only
from actual execution; a template is not evidence that a new context exists.
After an upgrade, use a new empty export directory to obtain updated schemas
without overwriting prior outputs. Existing task IDs and receipts stay valid.

Set a truthful actor ID, actor kind, and context ID. Different workers in one
loop/scope cannot share a context ID; a worker can continue its own context.
Unknown actual model, effort, and usage are null. Fake actors are used only in
offline protocol fixtures.

```bash
<skill-dir>/scripts/arc-runtime ac-llm host-submit \
  --run-root <run-root> --run-id <run-id> --response <response-file>
```

Submit is durable and idempotent for the same response. Conflicting responses,
wrong bindings, invalid schemas/JSON, duplicate keys, and non-finite values are
rejected. Submission does not commit a scientific round: the owning resume must
consume it through the original AC acceptance and ARC referee paths.

## Phase 3: Resume the original workflow

For a standalone LLM request use `ac-llm resume`; for paper/domain runs use the
owning package's documented resume. Rerun the same `run-calculate.py` config,
`run-ideas.py` config, or `write-domain-manifest.py` project command to continue
their persisted work. Do not create a new attempt, round, project, or scientific
step simply because the current call waits for Host.

Inspect public `host-pending --all`, batch `inspect` / `trace`, and `show-round`
to check actor/model metadata and committed results. Calculate returns
`arc.workflow.calculate.result.v4` with `awaiting_host` / `paused` plus a resume
descriptor. Existing bound v3 result state is still readable. Domain manifests
use v5, retaining cards while relationships pause; Ideas accepts existing v4
manifests. Ideas results use v6, exposing `research_status`, `pending_stages`,
and advisory resume information without erasing valid committed research.

Continue until completion, a real user-information requirement, stop, or
unavailable capability. Respect host call budgets and user steering. Stop
requests inhibit new submissions to that attempt; do not automatically start
a new attempt after a user stop. An explicit user continuation permits the
owning resume operation. Native internal model-session transfer is unsupported;
workflow state and accepted-prefix recovery are durable.

## Development and distribution checks

Use both `AC_FOUNDATION_REPO_ROOT` and `AC_PRODUCT_REPO_ROOT` with
`AC_INSTALL_SOURCE=local` for a complete source override. Set an isolated
`AC_RUNTIME_HOME` under ignored `local/` for developer smoke tests. The regular
immutable runtime is separate. A new Host implementation is not shipped until
approved runtime pins select reachable tested sources; old locks must not be
claimed to contain new code. Use `arc-runtime doctor` for source/runtime
identity and `ac-llm doctor` for the selected execution route.

Offline fixtures cover export/submit/resume, process interruption, calculator
independence, referee dependency order, and original commits. Work, Cowork,
Kimi, and dot account/tool availability, per-worker model control, and installation
need separate real-host tests. Local dot skills also require the appropriate
connected computer. No remote MCP deployment or external paid model API is
part of this workflow.
