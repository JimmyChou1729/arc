# ARC Operating Reference

This reference contains general operating rules for ARC workflows. Package
commands live in the package-specific references.

## General Rules

- Prefer cache reads first; generate or refresh only when needed.
- Use structured CLI output when available.
- Paper IDs may omit the `arXiv:` prefix.
- For slow or large work, use the owning package's durable controls and the
  host's background-command facility; see `manuals/arc-jobs.md`.
- Use the package CLI with structured output by default.
- The `arc` plugin is CLI-only and does not register or ship an MCP server.
- For user choices and confirmations, use
  `rules/interaction.md`.
- Always honor an explicit user stop. Otherwise, never stop for elapsed time,
  workload size, or temporary quiet alone. A workflow may request a cooperative
  stop when successive public status snapshots show the same recurring error
  or no goal-directed progress; record the snapshots and the concrete reason.
- Report cache paths or artifact paths when they help the user inspect results.
- The scientific integrity and robustness rules in
  `rules/integrity.md` apply to all ARC workflows.

## Reference Selection

### Phase 1: Identify the package surface.
Step 1: For single-paper work, read `manuals/arc-paper.md`.
Step 2: For domain or research-field work, read
`manuals/arc-domain.md`.
Step 3: For durable run inspection or stop requests, read
`manuals/arc-jobs.md`.
Step 4: For provider/model/runtime diagnosis, read
`manuals/arc-llm.md`.

### Phase 2: Execute through ARC.
Step 1: Use ARC package tools instead of scraping arXiv/INSPIRE directly.
Step 2: Keep generated or refreshed work explicit.
Step 3: Preserve warning and artifact contracts from
`rules/integrity.md`.
