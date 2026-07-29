# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion as a run-owned
`arc-render` publication and a standalone HTML reader. Build and resume require
public `arc-translate`, proposer-reviewer, paper, LLM, jobs, and render
dependencies; incomplete runtimes return `runtime_dependency_missing`.

## Build from a Local Rich Source

Use Markdown, HTML, or flattened single-file TeX as the authoritative source.
A PDF, when supplied, is an input validator for fidelity and page mapping; it
is never a reader delivery or a publication artifact.

```bash
arc-companion build <source.md> \
  --pdf <validator.pdf> \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

Use the user-chosen `<project-dir>` itself; do not append an attempt suffix.
Inside this checkout use a stable ignored path below `local/`. Companion claims
only `.arc/companion/` and the root `companion.html`; unrelated files remain
untouched.

For a remote arXiv paper, `--pdf fetch` fetches a PDF validator only:

```bash
arc-companion build <arxiv-id> \
  --pdf fetch \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

Set `<host-authority>` once per run: use `unrestricted` only when explicitly
reported; otherwise use `unknown`. Reuse it for every resume. For `restricted`
or `unknown` host requests, follow `manuals/arc-llm.md`.

The build reuses verified work within its own durable run. After the
`arc-translate` glossary barrier, translation completes before reviewed guide
generation. Each guide receives only glossary entries deterministically
matched to its chapter. The glossary size is approximate.

Companion does not run a document-wide literature survey before guide
generation. Instead, each chapter proposer and reviewer may research for a
concrete need and add useful references, with no minimum or maximum reference
count. Agents prefer cached `arc-paper` resources, admit newly acquired DOI,
arXiv, URL, local-file, book, and other resources when possible, and may use
currently available, authorized host research or download tools without
requiring new installation, connection, or authorization. Model JSON carries
handles and semantic results, while bodies remain in the cache or text-only
workspace. Only English Wikipedia is accepted; translated notes and excerpts
retain the English page title and URL.

Paragraph-local and cross-paragraph units have equal status and no quota.
Retain distinct explanatory value; remove generic summary, paraphrase, and
repetition. Prose is free CommonMark: ARC constrains anchors, evidence,
coverage, and renderability, not explanatory form.

The proposer reads the complete chapter and checks every source part; the
reviewer compares the proposal with the same original and frozen translation.
Absent a user audience, general writing targets a non-specialist adult,
research targets students with foundations, and textbooks target students
with prerequisites without presuming hard topics are mastered. No count quota
applies. Corrective contrast requires a misconception established by source or
evidence.

Every chapter proposal, including an empty proposal, enters
`arc-proposer-reviewer`. The reviewer accepts immediately when no concrete
improvement exists, or gives constructive feedback for at most two complete
revisions, including useful new anchored ideas and references. The maximum is
proposer-reviewer-proposer-reviewer-proposer, with no unused final review;
Companion injects the chapter ID.

Without `--paper-cache-root`, Companion uses ARC's shared paper cache. Durable
project state remains under `<project-dir>/.arc/companion/`.

## Inspect, Resume, Validate, and Render

```bash
arc-companion status --project-dir <project-dir>
arc-companion resume --project-dir <project-dir> --input <resume-input.json> \
  --host-authority <host-authority>
arc-companion stop --project-dir <project-dir> --reason "<reason>"
arc-companion validate --project-dir <project-dir>
arc-companion render --project-dir <project-dir>
```

Omit `--input` when the current pause descriptor does not require it. Resume
the same project and run lineage so accepted child work can replay.

Each successful build is materialized below
`<project-dir>/.arc/companion/publications/<run-id>/` as an `arc-render`
publication with native Layers, immutable Markdown fragment revisions, and
resources. Build and resume automatically attempt to render that publication
to the root standalone `<project-dir>/companion.html`.

`validate` checks the run-owned publication and its standalone HTML together.
`render` is model-free: it rematerializes the selected publication and rewrites
the standalone HTML after a renderer, style, font, or validator change.

ARC does not create or publish Companion PDFs. A person can open
`companion.html` in Chrome and use Print / Save as PDF. That PDF is a
user-side derivative, not an ARC release artifact: ARC does not validate,
reproduce, automatically publish, or make durability guarantees for it.

## Help

```bash
arc-companion --help
arc-companion <command> --help
```
