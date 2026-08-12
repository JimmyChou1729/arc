# ARC Domain Quick Start

`arc-domain` builds a durable research-domain package from one seed paper and
a scientific intent. Use it when the desired result is a field summary,
citation graph, evidence pack, or paper pack. Use the domain workflow for
multiple seeds, origin selection, project-local exports, and handoff to idea
generation.

## Build and Read a Domain

```bash
arc-domain build <seed-paper> --intent "<scientific intent>" \
  --project-dir <project-dir> \
  --host-authority <host-authority>
```

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value for every resume of that run. For `restricted` or `unknown`
host requests, follow `manuals/arc-llm.md`; do not assume a universal broker.

The result reports both a durable `run_id` and a stable `domain_id`. Keep both:
the run ID controls this attempt, while the domain ID selects the published
domain.

```bash
arc-domain status --project-dir <project-dir> --run-id <run-id>
arc-domain status --project-dir <project-dir> --domain-id <domain-id>
arc-domain get-summary --project-dir <project-dir> --domain-id <domain-id>
arc-domain get-graph --project-dir <project-dir> --domain-id <domain-id>
```

For a date-bounded field request, the managed workflow freezes `as_of_date` and
passes the corresponding `recent_window_days`; the date window applies to the
citer corpus rather than silently changing the canonical origin.

## Resume and Validate

```bash
arc-domain resume <run-id> --project-dir <project-dir> --host-authority <host-authority>
arc-domain validate <run-id> --project-dir <project-dir>
arc-domain stop <run-id> --project-dir <project-dir> --reason "<reason>"
```

Resume the same run after a pause or a failed attempt. Pass `--input` only when
a paused run's typed resume descriptor requires it. For a failed run, inspect
`last-error.json` and the working paths returned by status. Correct a candidate
or artifact to adopt it, or delete it to regenerate it, then resume explicitly.
If an upstream input changes, delete downstream working files that should be
rebuilt. ARC does not automatically retry or classify recovery causes. A stop
is resumable; it is not a failed replacement run. The project directory is
required: ARC stores durable domain state below `<project-dir>/.arc/domain`.
Read the returned status, warnings, and published artifact references after
every attempt.
The `arc-paper` cache defaults below the launch directory and can be overridden
with `--paper-cache-root` on build and resume.

The managed domain workflow records visible summary warnings and publishes
`arc.workflow.domain_manifest.v4` only after verified exports. That manifest
references the content-addressed `arc.workflow.domain_seed_provenance.v1`
artifact and preserves every domain package before an ideas workflow starts.
Its pairwise `domain_relationships` are advisory evidence with confidence and
warnings; they never choose an Ideas route. If relationship analysis is
unavailable, the manifest remains usable and Ideas proceeds from the package
cards with a visible warning.

## Help

Use help for policy controls, strict date windows, model selection, project
state location, shared-paper-cache selection, and exact result semantics:

```bash
arc-domain --help
arc-domain build --help
arc-domain <command> --help
```
