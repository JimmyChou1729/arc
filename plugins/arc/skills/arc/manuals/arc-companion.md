# ARC Companion Quick Start

`arc-companion` builds a source-anchored reading companion with translation,
chapter guides, a searchable PDF, and a static Web reader. Use the Companion
workflow for a complete managed run; use this manual for its package commands.

Build and resume require the public `arc-translate` facade and the declared
paper, LLM, and jobs dependencies. An incomplete runtime returns
`runtime_dependency_missing` before creating or changing a project.

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
project's exact source-compatible language, glossary, and chapter translations.
Companion verifies them against the accepted book and copies them into
target-owned state without invoking a translation provider.

Use the directory explicitly chosen by the user as `<project-dir>` itself;
do not append `companion`, `build-v2`, `fresh`, or an attempt-specific suffix.
Inside the ARC checkout, choose a stable ignored path below `local/`. This
`local/` convention does not apply to an external directory supplied by the
user.

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value for every resume of that run. For `restricted` or `unknown`
host requests, follow `manuals/arc-llm.md`; do not assume a universal broker.

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

The build reuses compatible verified source, glossary, translation, and
chapter artifacts. Language detection and translation belong to
`arc-translate`; after the glossary barrier, translation and guide generation
may proceed in parallel. The glossary size is approximate.

Before chapter planning, Companion requests one document-wide log of at least
20 candidate works or discussions across named sources, prior history, and
central later debates. This is an inspection floor, not a citation target;
only directly relevant selected evidence reaches planning and bibliography.

Paragraph-local and chapter-level/cross-paragraph units have equal status and
no placement quota. Retain only distinct motivation, presentation,
implication, omitted reasoning, connection, reliable context, or useful later
development. Remove generic summary, paraphrase, and repeated source reasoning.

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
