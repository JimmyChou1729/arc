# arc-companion

`arc-companion` owns source-anchored chapter-guide orchestration,
deterministic joining of translation and guide lanes, rendering, release
publication, and validation. It consumes verified documents from `arc-paper`
and reusable language, glossary, and translation results from `arc-translate`.
Its public build API is the split translation/guide workflow:
`CompanionBuildRequest`, `CompanionGenerationRecipe`, and
`CompanionBuildHandler` are the sole durable build lineage. Published content
uses the `arc.companion.accepted_book.v5` delivery contract and embeds the
current RichDocument v2 inline-span payloads directly.

A build or resume requires the complete installed runtime, including the
public `arc-translate`, `arc-paper`, `arc-llm`, `arc-jobs`, and
`arc-proposer-reviewer` packages.
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
The prior accepted Companion is copied with them and supplied to planning,
drafting, and review as optional reference context. It may improve, extend,
recombine, or discard prior ideas; old structure and bibliography are not
templates or current evidence.

Use repeatable `--author <name>` only for a user-confirmed author. Otherwise
Companion extracts possible author names from source metadata and bylines, then
asks the model to publish them only at high confidence; uncertain names are
omitted. Use `--reader-labels <json>` to supply the complete reader-interface
vocabulary for a target language that ARC does not ship.

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

A successful build or resume formally publishes and validates an immutable
Web release and attempts the optional PDF rendering. A PDF rendering or
validation failure is reported as `pdf_render_failed`; it does not change the
successful build status or prevent publication of the Web reader. The
model-free `render` command is the manual republication path for an already
accepted book after renderer, font, style, or validation changes; its
`--format` option only filters reported artifacts.

Every successful publication refreshes the managed project-root
`companion.html` delivery copy. When PDF rendering succeeds it also refreshes
`companion.pdf`, which is byte-for-byte the canonical release PDF. A Web-only
publication removes any stale managed PDF so it cannot be mistaken for part of
the current release. The HTML is a standalone offline file: its local styles,
scripts, fonts, and frozen source assets are embedded as data URIs, so it may
be copied outside the project without breaking reader behavior. External
source and bibliography links remain ordinary navigation links. Use
`arc-standalone-html INPUT.html OUTPUT.html` to create the same deterministic
offline representation for another local HTML bundle. Command artifacts and
data report only the formats and delivery paths that were actually published.
Durable runs, diagnostics, and frozen source assets are stored under
`<project-dir>/.arc/companion/`. Paper data remains in ARC's shared reusable
paper cache unless `--paper-cache-root` selects another shared cache. Figure
assets needed by a completed release are frozen into the project before
publication completes, so later rendering does not depend on cache retention.

Whole-document model tasks do not receive the source body inside prompt JSON.
Companion freezes a compact chapter/block index and a verified text-only
Markdown view as `arc-llm` workspace inputs. In direct mode, the index tells a
worker how to query the exact immutable `arc-paper` cached-document handle for
only the sections or search hits it needs. If that cache interface is
unavailable, the worker reads the text-only view instead; image and media bytes
are never model inputs. Chapter IDs remain program-owned routing data, while
model-authored plans and guide proposals remain schema-validated JSON.

The project layout is stable:

```text
<project-dir>/
  companion.pdf
  companion.html
  releases/<release-id>/...
  .arc/companion/
    jobs/<run-id>/working/
      semantic-input.json
      index.json
      artifacts/...
      candidates/...
      last-error.json
    diagnostics/visual/<run-id>/...
    operator-inputs/<run-id>/...
    frozen-assets/...
```

Only `working/` is agent-editable recovery state. Candidate model outputs are
written there before Companion's deterministic identity, reference, coverage,
and rich-text checks. When a schema-valid output first fails one of those
checks, Companion preserves it, gives the concrete validation feedback to one
fresh model task, and writes that task's answer to a separate
`*.semantic-retry.json` candidate. If the retry also fails, the build pauses
with both paths visible instead of treating the model-correctable output as a
terminal failure. An agent may edit the active retry candidate and resume
without another provider call. Removing an exhausted candidate does not grant
a third automatic generation attempt. These checks support model repair; they
are not additional scientific-content gates.

Guide writing uses `arc-proposer-reviewer`, not a Companion-specific patch
loop. A chapter starts with a complete proposal. A reviewer may accept it
immediately when there is no concrete improvement to make; otherwise it gives
constructive, actionable feedback for at most two complete revisions. The
maximum sequence is proposer-reviewer-proposer-reviewer-proposer. The final
revision goes directly to deterministic anchor, evidence, coverage, and
rich-text validation, so no unused final review is generated. Companion writes
the caller-owned chapter ID into the final candidate rather than asking the
model to generate routing identity.

Immutable releases, snapshots, locks, and frozen content objects remain
ARC-managed.

Failed Companion attempts caused by provider, state, or final accepted-book
invariants may be explicitly resumed after inspecting or repairing the selected
run's `working/` state. Each failed resume uses a new recovery epoch while
preserving readable successful work. The active PDF, HTML, and current release
pointer are not replaced until the recovered build fully succeeds and the
replacement release validates.

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

Planning audits every source block against a type-aware default reader.
Popular or directional writing assumes a generally educated adult without
specialist training. Research papers assume a professional student who has
completed the relevant foundational courses. Textbooks assume a student who
has completed the standard prerequisites, without assuming that difficult
prerequisite material is already mastered. Explicit reader background in the
user intent overrides these defaults.

Reader HTML marks glossary terms with a quiet gray underline in the source,
translation, guide titles, and guide prose; hover and keyboard focus expose
the glossary explanation. Ordinary HTML links use the same low-emphasis
underline treatment. PDF glossary terms use a subtle gray underline with no
tooltip annotation or blue text, preserving line height and text extraction.
Source page numbers remain available as internal provenance but are not
printed in reader-facing output.

## Tests

The default suite is offline and uses fake model services:

```bash
python -m pytest packages/arc-companion/tests
```
