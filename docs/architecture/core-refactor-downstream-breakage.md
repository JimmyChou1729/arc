# ARC v1 Core Refactor Migration Report

This is the final migration report for the ARC v1 core refactor. It records
the completed package and Skill boundaries, the frozen pre-v1 test-ledger
closure, and the verification completed before final closure-fixture removal.
It is not a compatibility backlog: retired APIs and durable layouts must not
be restored as shims.

## Baseline and Scope

- Frozen baseline: `f7b4424f427a11f40cd3dea61cb56e90067685dd`
  (`f7b4424`).
- ARC remains six independently installable packages:
  `arc-jobs`, `arc-llm`, `arc-proposer-reviewer`, `arc-paper`, `arc-domain`,
  and `arc-companion`.
- The Skill and plugin launchers invoke documented package CLIs or public
  Python APIs. Package source does not derive runtime behavior or configuration
  from Skill, plugin, or workflow files.
- No workflow package was introduced. Agent-facing deterministic orchestration
  lives under `plugins/arc/skills/arc/scripts/`, with reusable implementation
  modules under `scripts/_arc_workflows/`.

## Completed Public Boundaries

### Durable execution and model calls

`arc-jobs` owns durable execution, run state, and generic control operations.
`arc-llm` owns provider selection and durable typed LLM tasks. Its supported
CLI is `generate`, `resume`, `status`, `cancel`, and `doctor`; prior direct
runner and controller surfaces are retired.

`arc-proposer-reviewer` owns typed multi-worker proposer/reviewer batches. Its
public observation surface is the read-only projection:

```text
inspect_batch(repository, run_id)
read_batch_trace(repository, run_id)
read_batch_round(repository, run_id, loop_id, round_number)
```

The corresponding CLI commands are `inspect`, `trace`, and `show-round`.
Inspection is available at every batch lifecycle, but its activity counts are
best effort and cannot drive ranking, recovery, retry, or resume. Trace shows
only verified, loop-atomically committed rounds and public logical refs;
published but uncommitted partial artifacts are invisible. A run revision and
per-loop revision vector identify an observation without claiming a globally
linearized snapshot. Only `show-round`/`read_batch_round` expands committed
proposal and review JSON. Sessions, task IDs, private group IDs, full pause
records, and physical paths are not public projection data. Corrupt committed
data fails closed for trace and round expansion.

### Paper data and cache-first deep document operations

`arc-paper` owns deterministic paper access, cache-backed source acquisition,
parsing, and deep document queries. The public arXiv operations accept a
canonicalizable arXiv identifier directly and use the shared paper cache:

```text
get-arxiv-table-of-contents
get-arxiv-section
search-arxiv-full-text
search-arxiv-equations
```

The parsed-document cache is derived from canonical source content identity
plus the explicit parser contract. It verifies source and payload identities,
reuses entries across service instances, and rebuilds a corrupt derived entry
from a verified source. It does not fall back silently to PDF when ar5iv
acquisition or parsing fails.

### Skill workflows

The migrated Skill scripts consume the documented package contracts. Ideas and
calculation ranking read only verified committed proposer-reviewer rounds;
they do not parse private loop directories, worker sessions, or artifact paths.
The ideas evidence resolver is a bounded `arc-paper` operation allowlist, not
the retired evidence controller. The calculation runner creates deterministic,
independent batches per attempt and preserves its blind-reference, locked
output, budget, human-pause, and work-note handoff behavior.

The domain manifest helper uses typed `LLMClient.generate` for its one
multi-package field-grouping request. It neither imports the retired LLM
controller API nor moves Skill-specific grouping policy into `arc-domain`.

## Retired Surfaces

- `arc-mcp` is absent from package source, plugin source, launchers, runtime
  profiles, and dependency constraints. ARC is CLI-and-Skill only; no ARC-owned
  MCP adapter, compatibility shim, or state migration remains.
- The old LLM CLI/runner API, controller API, and controller-generated evidence
  flow are retired. Typed task requests and explicit pauses replace those
  surfaces.
- The old evidence controller is retired. Workflow evidence is supplied only
  through the bounded, typed resolver contract.
- Private artifact-layout parsing is retired as a workflow observation method.
  Public projections and their verified logical references are the supported
  observation boundary.

## Frozen Pre-v1 Test-Ledger Closure

The retained fixtures under `tests/architecture/fixtures/` are the auditable
closure record for the frozen baseline. Every group is closed. Counts below are
exact fixture counts, grouped by disposition.

| Ledger | Closed groups | Retain | Rewrite | Move | Delete |
| --- | ---: | ---: | ---: | ---: | ---: |
| `arc-jobs` | 62 | 3 | 16 | 9 | 34 |
| `arc-llm` | 581 | 0 | 72 | 2 | 507 |
| `arc-proposer-reviewer` | 126 | 18 | 30 | 13 | 65 |
| `arc-paper` | 409 | 11 | 96 | 38 | 264 |
| **Total** | **1,178** | **32** | **214** | **62** | **870** |

The fixtures and `tests/architecture/test_legacy_test_audit.py` remain until
the final migration report is complete and the full-ledger commit SHA is
recorded below. Their removal is a separately committed retirement step; they
are not package runtime state and do not provide a compatibility layer.

## Implemented Commit Sequence

The completed implementation through runtime/workflow documentation alignment
is represented by these commits, in order:

1. `35ec56bfbcdc2375dd5a1a30d603a6531399f3fa`
   `docs(architecture): enforce agent-package boundaries`
2. `1f08ecb368771f2a2ef8322fd5e78ad673b55a49`
   `feat(paper): add cache-first arxiv document queries`
3. `17e14bc593acc3de0f7b8531e9ec96e4d4c9af84`
   `feat(proposer-reviewer): add live inspection and trace`
4. `ea239f6c6592a2113b69070ddc63d842d17e48dd`
   `refactor(skill): migrate ARC workflow scripts`
5. `cd7ac8f309cb47de3d626689da896c1da202e71c`
   `docs(arc): align runtime and workflow contracts`

## Verification Recorded Before Final Closure

The following bounded checks were completed during the implementation before
the final combined verification pass:

- `tests/test_arc_research_workflow_docs.py`: 91 passed.
- Focused core-launcher checks: 3 passed, 16 deselected.
- `packages/arc-proposer-reviewer/tests/test_cli.py` plus
  `packages/arc-proposer-reviewer/tests/test_projection.py`: 14 passed.
- `tests/test_domain_manifest.py`: 14 passed.
- `tests/test_calculate_runner.py`: 11 passed.
- `tests/test_arc_source_provenance.py`: 13 passed, 1 skipped.
- `tests/architecture/test_package_dependencies.py`: 6 passed.
- All seven moved Skill scripts accepted `--help` from a source checkout with
  an empty `PYTHONPATH`.
- `arc-proposer-reviewer --help` completed successfully.
- Focused diff checks and Skill-tree bytecode checks completed without errors.

The retained archival audit is not a final runtime acceptance suite. At this
report's preparation, `tests/architecture/test_legacy_test_audit.py` passed all
8 cases against the frozen inventories. It remains an archival completeness
guard until the planned final ledger-retirement commit removes the closure
fixtures and its dedicated audit test.

## Final Verification

- Six-package suite: **789 passed, 1 skipped**.
- Repository `tests` suite: **215 passed, 3 skipped**.
- Architecture, identity, and protocol command: **85 passed**.
- Restored legacy audit: `tests/architecture/test_legacy_test_audit.py`:
  **8 passed**.
- `scripts/check-packages.sh` built both wheel and sdist successfully for all
  six `1.0.1` packages.
- Isolated wheel-target imports of all six packages passed.
- All seven moved Skill scripts accepted `--help`.

### Bounded Provider Smoke

Provider doctor found Codex available. Exactly one configured batch with one
loop, one round, proposer, and reviewer was attempted. The run paused in the
reviewer with `recovery_limit_reached`; no retry was attempted under the smoke
bound. `inspect` and `trace` succeeded and reported zero committed rounds.
`show-round` failed closed with `committed_round_not_found`.

This is a bounded safe-pause outcome, not a green completion.

### Remaining Finalization Records

- **Last commit containing the complete four-ledger closure:**
  `PENDING — replace with the exact report-commit SHA after this report is committed.`
- **Final clean-worktree and retired-import scan:**
  `PENDING — exact command and result after fixture retirement and install-ref pin.`

## Release and Publication Boundary

This migration does not run `scripts/release-arc.sh`, create a tag, push a
branch, or push tags. No version declaration, plugin manifest version, runtime
constraint, or install reference is changed by this report.
