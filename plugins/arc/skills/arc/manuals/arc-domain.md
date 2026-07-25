# ARC Domain Quick Start

`arc-domain` builds a durable research-domain package from one seed paper and
a scientific intent. Use it when the desired result is a field summary,
citation graph, evidence pack, or paper pack. Use the domain workflow for
multiple seeds, origin selection, project-local exports, and handoff to idea
generation.

## Build and Read a Domain

```bash
arc-domain build <seed-paper> --intent "<scientific intent>"
```

The result reports both a durable `run_id` and a stable `domain_id`. Keep both:
the run ID controls this attempt, while the domain ID selects the published
domain.

```bash
arc-domain status --run-id <run-id>
arc-domain status --domain-id <domain-id>
arc-domain get-summary --domain-id <domain-id>
arc-domain get-graph --domain-id <domain-id>
```

For a date-bounded field request, the managed workflow freezes `as_of_date` and
passes the corresponding `recent_window_days`; the date window applies to the
citer corpus rather than silently changing the canonical origin.

## Resume and Validate

```bash
arc-domain resume <run-id>
arc-domain validate <run-id>
arc-domain stop <run-id> --reason "<reason>"
```

Resume the same run after a pause. Pass `--input` only when its typed resume
descriptor requires it. A stop is resumable; it is not a failed replacement
run. Read the returned status, warnings, and published artifact references
instead of inspecting cache directories.

The managed domain workflow records visible summary warnings and publishes
`arc.workflow.domain_manifest.v3` only after verified exports. That manifest
references the content-addressed `arc.workflow.domain_seed_provenance.v1`
artifact before an ideas workflow starts.

## Help

Use help for policy controls, strict date windows, model selection, cache
location, and exact result semantics:

```bash
arc-domain --help
arc-domain build --help
arc-domain <command> --help
```
