# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
guides, searchable PDF, and Web reader. Use this manual for package commands.

Build and resume require public `arc-translate`, paper, LLM, and jobs dependencies; an incomplete runtime returns `runtime_dependency_missing`.

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

Add `--reuse-translation-from <existing-project-dir>` to preserve a successful
project's exact source-compatible language, glossary, and translations without
invoking a translation provider. Companion also copies the prior accepted guide
as optional model context: the model may improve, recombine, or discard old
ideas; its structure and bibliography are not current constraints.

Use the user-chosen `<project-dir>` itself; do not append `companion`, `build-v2`,
`fresh`, or an attempt suffix. Inside this checkout, use a stable ignored path
below `local/`; that convention does not apply to an external user directory.

Set `<host-authority>` once per run: use `unrestricted` only when explicitly
reported; otherwise use `unknown`. Reuse it for every resume. For `restricted`
or `unknown` host requests, follow `manuals/arc-llm.md`.

The root may contain unrelated user files; Companion preserves them. It claims
only `.arc/companion/`, `releases/`, `companion.pdf`, and `companion.html`, and
refuses exact conflicts there. Command JSON may use any unrelated path, but
never a managed Companion path.

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
artifacts. After the `arc-translate` glossary barrier, translation and guide
generation may proceed in parallel. The glossary size is approximate.

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

Open `<project-dir>/companion.pdf` or `companion.html` after publication. The
PDF is an exact copy; HTML points to the immutable release so assets and links
remain valid. The release manifest and CLI artifacts are authoritative. Do not
place unrelated files at either managed root path.

Companion freezes every source figure asset needed for its accepted release in
the project runtime before publication completes. A completed project can
therefore render and validate after the corresponding shared paper-cache entry
has been removed.

## Help

```bash
arc-companion --help
arc-companion <command> --help
```

Use help for target-language defaults, workers, glossary size, provider/model
selection, refresh behavior, and typed build failures.
