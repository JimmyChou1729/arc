# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
chapter guides, a searchable PDF, and a static Web reader. Use the Companion
workflow for a complete managed run; use this manual for its package commands.

## Build from a Local Rich Source

Use Markdown, HTML, or flattened single-file TeX as the authoritative source.
A PDF validator checks fidelity and supplies page mapping:

```bash
arc-companion build <source.md> \
  --pdf <validator.pdf> \
  --project-dir <project-dir> \
  --target-language <language-tag>
```

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
model-free and consumes only accepted artifacts. The accepted
compatibility-only `--json` flag does not change typed JSON output.

## Help

```bash
arc-companion --help
arc-companion <command> --help
```

Use help for target-language defaults, workers, glossary size, provider/model
selection, refresh behavior, and typed build failures.
