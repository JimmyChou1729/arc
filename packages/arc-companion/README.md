# arc-companion

`arc-companion` owns source-anchored chapter-guide orchestration,
deterministic joining of translation and guide lanes, rendering, release
publication, and validation. It consumes verified documents from `arc-paper`
and reusable language, glossary, and translation results from `arc-translate`.
Its public build API is the split translation/guide workflow:
`CompanionBuildRequest`, `CompanionGenerationRecipe`, and
`CompanionBuildHandler` are the sole durable build lineage. Published content
uses the `arc.companion.accepted_book.v2` delivery contract and embeds the
current RichDocument v2 inline-span payloads directly.

A build or resume requires the complete installed runtime, including the
public `arc-translate`, `arc-paper`, `arc-llm`, and `arc-jobs` packages.
Companion checks the translation facade before creating a new project or
mutating a resumed run and reports `runtime_dependency_missing` when that
installation is incomplete. Explicitly injected translation adapters remain
available for embedding and offline tests.

## Quick start

Build a companion from a rich source or paper identifier:

```bash
arc-companion build note.md \
  --project-dir local/example/companion \
  --target-language zh-CN \
  --user-intent "Explain the main argument and its assumptions." \
  --host-authority <host-authority>
```

On the first command, redirect JSON output outside the new project directory.
Shell redirection creates its target before Companion starts, so writing to
`local/example/companion/result.json` would make that otherwise-new directory
nonempty and correctly trigger unknown-state refusal.

Use `arc-companion --help` and `arc-companion build --help` for supported
sources, optional PDF validation, durable controls, rendering, and release
validation.

Build and resume accept `--host-authority` for all model-backed lanes. Set
`<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value when resuming. This runtime setting does not change the
Companion build recipe or release artifacts.

A successful build or resume formally publishes and validates one complete
immutable PDF/Web release. The model-free `render` command is the manual
republication path for an already accepted book after renderer, font, style,
or validation changes; its `--format` option only filters reported artifacts.
Every successful publication also refreshes the managed project-root
`companion.pdf` and `companion.html` delivery copies. The PDF is byte-for-byte
the canonical release PDF. The HTML contains a base link to the canonical
`releases/<release-id>/reader/index.html`, so reader assets and fragment links
continue to resolve inside the immutable release. Command artifacts still
identify the project-root PDF and HTML delivery files; command data reports
the two delivery paths. Durable runs, diagnostics, and frozen source assets
are stored under `<project-dir>/.arc/companion/`. Paper data remains in ARC's
shared reusable paper cache unless `--paper-cache-root` selects another shared
cache. Figure assets needed by a completed release are frozen into the project
before publication completes, so later rendering does not depend on cache
retention.

## Tests

The default suite is offline and uses fake model services:

```bash
python -m pytest packages/arc-companion/tests
```
