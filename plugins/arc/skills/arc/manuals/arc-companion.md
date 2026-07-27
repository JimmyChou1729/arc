# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
guides, searchable PDF, and Web reader. Build and resume require public
`arc-translate`, proposer-reviewer, paper, LLM, and jobs dependencies;
incomplete runtimes return `runtime_dependency_missing`.

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
`releases/`, `companion.pdf`, and `companion.html`; command JSON must avoid them.

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

Companion does not run a document-wide literature survey before planning.
Instead, each chapter proposer and reviewer may research for a concrete need
and add useful references, with no minimum or maximum reference count. Agents
prefer cached `arc-paper` resources, admit newly acquired DOI, arXiv, URL,
local-file, book, and other resources when possible, and may use currently
available, authorized host research or download tools without requiring new
installation, connection, or authorization. Model JSON carries handles and
semantic results, while bodies remain in the cache or text-only workspace.
Only English Wikipedia is accepted; translated notes and excerpts retain the
English page title and URL.

Paragraph-local and cross-paragraph units have equal status and no quota.
Retain distinct explanatory value; remove generic summary, paraphrase, and
repetition. Prose is free CommonMark: ARC constrains anchors, evidence,
coverage, and renderability, not explanatory form.

Planning audits every source block. Absent a user audience, general writing
targets a non-specialist adult, research targets students with foundations, and
textbooks target students with prerequisites without presuming hard topics are
mastered. Required needs remain covered; no count quota applies. Corrective
contrast requires a misconception established by source or evidence.

Every chapter proposal, including an empty proposal, enters
`arc-proposer-reviewer`. The reviewer accepts immediately when no concrete
improvement exists, or gives constructive feedback for at most two complete
revisions, including useful new anchored ideas and references. The maximum is
proposer-reviewer-proposer-reviewer-proposer, with no unused final review;
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

## Help

```bash
arc-companion --help
arc-companion <command> --help
```
