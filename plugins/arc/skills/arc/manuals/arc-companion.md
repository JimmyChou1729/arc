# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
guides, searchable PDF, and Web reader. Use this manual for package commands.

Build and resume require public `arc-translate`, proposer-reviewer, paper, LLM,
and jobs dependencies; incomplete runtimes return `runtime_dependency_missing`.

## Build from a Local Rich Source

Use Markdown, HTML, or flattened single-file TeX as the authoritative source.
A PDF validator checks fidelity and supplies page mapping:

```bash
arc-companion build <source.md> \
  --pdf <validator.pdf> \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

Add `--reuse-translation-from <existing-project-dir>` to preserve exact
source-compatible language, glossary, and translations without invoking a
translation provider. The prior guide is optional context, not a template or
current evidence.

Use the user-chosen `<project-dir>` itself; do not append an attempt suffix.
Inside this checkout use a stable ignored path below `local/`.

Set `<host-authority>` once per run: use `unrestricted` only when explicitly
reported; otherwise use `unknown`. Reuse it for every resume. For `restricted`
or `unknown` host requests, follow `manuals/arc-llm.md`.

Companion preserves unrelated root files. It claims only `.arc/companion/`,
`releases/`, `companion.pdf`, and `companion.html`; command JSON must not use
those managed paths.

For a remote arXiv paper, let ARC fetch the PDF validator:

```bash
arc-companion build <arxiv-id> \
  --pdf fetch \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

The build reuses compatible verified source, glossary, translation, and chapter
artifacts. After the `arc-translate` glossary barrier, translation completes
before reviewed guide generation. Each guide receives only glossary entries
deterministically matched to its chapter. The glossary size is approximate.

Before planning, Companion researches at least 20 candidates across named
sources, prior history, and central later debates. This is an inspection floor,
not a citation target. It is a standard `arc-llm` task: unrestricted models use
research tools directly; other modes use native host turns. Companion has no
evidence resume input. Only English Wikipedia is accepted, and target-language
notes retain English page titles and URLs.

Paragraph-local and cross-paragraph units have equal status and no quota.
Retain distinct explanatory value; remove generic summary, paraphrase, and
repetition. Prose is free CommonMark: ARC constrains anchors, evidence,
coverage, and renderability, not explanatory form.

Planning audits every source block. Absent a user audience, general writing
targets a non-specialist adult, research targets students with foundations, and
textbooks target students with prerequisites without presuming hard topics are
mastered. Required needs remain covered; no count quota applies. Corrective
contrast requires a misconception established by source or evidence.

`arc-proposer-reviewer` accepts immediately when no concrete improvement
exists, or gives constructive feedback for at most two complete revisions,
including worthwhile anchored, evidence-backed ideas. The maximum sequence is
proposer-reviewer-proposer-reviewer-proposer; no unused final review follows.
Companion injects the chapter ID.

Without `--paper-cache-root`, Companion uses ARC's shared paper cache. Durable
project state remains under `<project-dir>/.arc/companion/`.

## Inspect and Recover

```bash
arc-companion status --project-dir <project-dir>
arc-companion resume --project-dir <project-dir> --input <resume-input.json> \
  --host-authority <host-authority>
arc-companion stop --project-dir <project-dir> --reason "<reason>"
```

Omit `--input` when the current pause descriptor does not require it. Resume
the same project and run lineage so accepted child work can replay.

## Validate and Render

```bash
arc-companion validate --project-dir <project-dir>
arc-companion render --project-dir <project-dir> --format all
```

Validation checks the accepted book and release. Rendering is model-free;
successful build and resume already publish a complete immutable PDF/Web
release. Use `render` after renderer, font, style, or validator changes;
`--format` filters reported artifacts, not what is validated and published.

Open `<project-dir>/companion.pdf` or `companion.html`. The PDF is exact; HTML
is standalone with local assets embedded and external source links preserved.
The release manifest and CLI artifacts are authoritative.

Companion freezes required source figures so completed projects remain
renderable after shared paper-cache removal.

## Help

```bash
arc-companion --help
arc-companion <command> --help
```
