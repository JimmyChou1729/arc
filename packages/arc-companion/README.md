# arc-companion

`arc-companion` owns source-anchored chapter-guide orchestration,
deterministic joining of translation and guide lanes, rendering, release
publication, and validation. It consumes verified documents from `arc-paper`
and reusable language, glossary, and translation results from `arc-translate`.
Its public build API is the split translation/guide workflow:
`CompanionBuildRequest`, `CompanionGenerationRecipe`, and
`CompanionBuildHandler` are the sole durable build lineage. Published content
uses the `arc.companion.accepted_book.v3` delivery contract and embeds the
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

Use `--reuse-translation-from <existing-project-dir>` to preserve the exact
verified language, glossary, and chapter-translation artifacts from a
successful project while producing a new guide. Reused bytes are copied into
target-owned state and checked against the source's final accepted book, so the
new project remains usable after the source project is unavailable.

Use the explicitly selected `--project-dir` as the project root itself. Keep a
stable name; do not append `build-v2`, `fresh`, or attempt-specific suffixes.
Inside the ARC checkout, project output belongs under ignored `local/`.
External user-supplied project directories do not need a `local/` component.

The root may already contain source material, notes, or other unrelated user
files. Companion preserves those files and claims only `.arc/companion/`,
`releases/`, `companion.pdf`, and `companion.html`. First initialization
refuses an exact conflict at one of those managed paths without modifying the
directory. JSON output may be redirected to any unrelated path, including one
inside the project root.

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

Failed Companion attempts may be explicitly resumed after inspecting or
repairing the selected run's `working/` state. Each failed resume uses a new
recovery epoch while preserving readable successful work. The active PDF,
HTML, and current release pointer are not replaced until the recovered build
fully succeeds and the replacement release validates.

Guide generation is evidence-first and selective. One document-wide research
log must inspect at least 20 distinct candidate works or substantive
discussions spanning source-named works, important prior history, and central
later debates. This is not an inclusion quota: only directly relevant selected
evidence can influence chapter planning or appear in the bibliography.
Paragraph-local notes and chapter-level or cross-paragraph notes have equal
status, with placement chosen case by case. Units that merely summarize,
paraphrase, or repeat the source are removed; retained units must add a
distinct motivation, presentation, implication, reasoning step, connection,
reliable context, or useful later development.

## Tests

The default suite is offline and uses fake model services:

```bash
python -m pytest packages/arc-companion/tests
```
