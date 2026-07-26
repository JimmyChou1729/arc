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
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

Use the directory explicitly chosen by the user as `<project-dir>` itself;
do not append `companion`, `build-v2`, `fresh`, or an attempt-specific suffix.
Inside the ARC checkout, choose a stable ignored path below `local/`. This
`local/` convention does not apply to an external directory supplied by the
user.

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value for every resume of that run. For `restricted` or `unknown`
host requests, follow `manuals/arc-llm.md`; do not assume a universal broker.

The project root may already contain source material, notes, and other
unrelated user files; Companion preserves them. On first initialization it
claims only `.arc/companion/`, `releases/`, `companion.pdf`, and
`companion.html`, and refuses an exact conflict at any of those paths without
changing the directory. Command JSON may be redirected to an unrelated path
inside the project root, but never to a managed Companion path.

For a remote arXiv paper, let ARC fetch the PDF validator:

```bash
arc-companion build <arxiv-id> \
  --pdf fetch \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

The build reuses compatible verified source, glossary, translation, and
chapter artifacts. Language detection and translation belong to
`arc-translate`; after the glossary barrier, translation and guide generation
may proceed in parallel. The glossary size is approximate.

`--paper-cache-root` is optional. Without it, Companion uses ARC's shared,
reusable paper cache. Companion's durable runs, diagnostics, and frozen source
assets are project-local under `<project-dir>/.arc/companion/`.

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
