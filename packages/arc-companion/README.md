# ARC Companion

`arc-companion` builds a source-anchored textbook companion from rich
Markdown, HTML, or single-file flattened TeX. The rich source is authoritative;
an optional PDF validates source alignment and supplies page mappings.

One durable build detects source language once, plans chapters from source
headings, creates one whole-book glossary, translates every source block when
the primary language differs, generates selective textbook notes, and runs one
text-only reviewer per chapter. The immutable `AcceptedBook` then drives both
the searchable PDF and responsive static Web reader.

## Commands

All commands write one `arc.command_result.v1` JSON document:

```bash
arc-companion build SOURCE --project-dir DIR \
  --target-language zh-CN --user-intent '...' \
  --provider auto --workers 4 --json

arc-companion status --project-dir DIR --json
arc-companion resume --project-dir DIR --input response.json --json
arc-companion cancel --project-dir DIR --json
arc-companion render --project-dir DIR --format all --json
arc-companion validate --project-dir DIR --json
```

For a local source, `--pdf PATH` supplies the optional validator. For a remote
paper ID, `--pdf fetch` asks `arc-paper` to fetch the PDF validator explicitly.
Without a PDF, the build continues and reports a technical diagnostic only in
the command result.

Use a new or empty project directory. Companion 1.0.1 does not migrate legacy
pipeline state and refuses a nonempty unmarked directory before source or
runtime writes. Existing legacy PDF/Web files remain untouched.

## Output

A successful build or render publishes:

```text
releases/<release-id>/companion.pdf
releases/<release-id>/reader/index.html
releases/<release-id>/manifest.json
current.json
```

The release ID depends only on the accepted-book digest, render recipes, and
validator contract. Runtime warnings, provider details, cache paths, run IDs,
and schema diagnostics are excluded from renderable content.
