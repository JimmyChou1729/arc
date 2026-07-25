# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
chapter guides, a searchable PDF, and a static Web reader. Use the Companion
workflow for a complete managed run; use this manual for its package commands.

Build and resume require a complete Companion runtime, including the public
`arc-translate` facade and the declared paper, LLM, and jobs dependencies. A
missing or incomplete translation installation returns the typed
`runtime_dependency_missing` error before a new project is created or a
resumed run is changed.

## Build from a Local Rich Source

Use Markdown, HTML, or flattened single-file TeX as the authoritative source.
A PDF validator checks fidelity and supplies page mapping:

```bash
arc-companion build <source.md> \
  --pdf <validator.pdf> \
  --project-dir <project-dir> \
  --target-language <language-tag>
```

For the first command, redirect the JSON result outside `<project-dir>`.
For example, use `> local/companion-build-result.json`, not
`> <project-dir>/result.json`: the shell creates a redirection target before
Companion can establish its project marker, and unknown nonempty directories
are deliberately refused.

For a remote arXiv paper, let ARC fetch the PDF validator:

```bash
arc-companion build <arxiv-id> \
  --pdf fetch \
  --project-dir <project-dir> \
  --target-language <language-tag>
```

The build reuses compatible verified source, glossary, translation, and
chapter artifacts. Language detection and translation belong to
`arc-translate`; after the glossary barrier, translation and guide generation
may proceed in parallel. The glossary size is approximate.

## Inspect and Recover

```bash
arc-companion status --project-dir <project-dir>
arc-companion resume --project-dir <project-dir> --input <resume-input.json>
arc-companion stop --project-dir <project-dir> --reason "<reason>"
```

Omit `--input` when the current pause descriptor does not require it. Resume
the same project and run lineage so accepted child work can replay.

## Validate and Render

```bash
arc-companion validate --project-dir <project-dir>
arc-companion render --project-dir <project-dir> --format all
```

Validation checks the accepted source-anchored book and release. Rendering is
model-free and consumes only accepted artifacts. Successful build and resume
commands already perform the formal publication of a complete immutable
PDF/Web release. Use `render` manually to republish that accepted book after a
renderer, font, style, or validator change; `--format` filters only the
artifacts reported to the caller, not what is validated and published.

After publication, open `<project-dir>/companion.pdf` or
`<project-dir>/companion.html` for convenient delivery. These are managed,
rebuildable projections: the PDF is an exact copy, while the HTML points its
base at `releases/<release-id>/reader/index.html` so canonical assets and links
remain in the immutable release. The release manifest and CLI artifacts remain
the authoritative immutable records. Do not place unrelated files at either
managed root path.

## Help

```bash
arc-companion --help
arc-companion <command> --help
```

Use help for target-language defaults, workers, glossary size, provider/model
selection, refresh behavior, and typed build failures.
