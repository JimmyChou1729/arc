# ARC Render Quick Start

`arc-render` composes a frozen `arc-paper` `RichDocument` and optional
source-bound overlay Layers into a standalone HTML reader. It does not discover
or parse a source itself.

## Run ARC Render

Use `arc-paper` and `arc-render` directly when they are on `PATH`. Check once:

```bash
arc-paper export-rich-document --help
arc-render --help
```

If a bare command is unavailable, use the portable Skill runtime:

```bash
<skill-dir>/scripts/arc-runtime arc-paper export-rich-document --help
<skill-dir>/scripts/arc-runtime arc-render --help
```

Inside an ARC source checkout, the package virtual environment is the direct
development fallback:

```bash
packages/arc-paper/.venv/bin/arc-paper export-rich-document --help
packages/arc-paper/.venv/bin/arc-render --help
```

Use the selected launcher in place of the bare command below. Do not search
package internals for another executable.

## Build a Source-Only Reader

First export a local Markdown, HTML, or flattened single-file TeX source into a
new or empty publication directory:

```bash
arc-paper export-rich-document note.md --output-dir publication
```

Add `--validator note.pdf` when a PDF should validate fidelity or page mapping.
The command creates `publication/rich-source.json`,
`publication/metadata.json`, and `publication/resources/`. It verifies and
copies source assets and writes their complete metadata, so do not copy figures
or hand-author a `resources` array afterward.

Compose the smallest valid publication with no overlays:

```bash
arc-render compose \
  --source publication/rich-source.json \
  --metadata publication/metadata.json \
  --output publication/publication.json
```

Render and validate the standalone reader:

```bash
arc-render render \
  --publication publication/publication.json \
  --html publication/reader.html

arc-render validate \
  --publication publication/publication.json \
  --html publication/reader.html
```

Keep `publication.json`, every Layer, every referenced fragment revision, and
all declared resources below the same publication root. Relative paths in the
publication are resolved from the directory containing `publication.json`.

## Add Overlay Layers

`--layer` is optional and repeatable; command-line order is publication order.
Every Layer file must be below the output publication directory and must bind
the exact `RichDocument`. Its referenced `fragments/` files must also be present
below that root. When reusing a standalone `arc-translate` result, copy both
`translation.layer.json` and its complete `fragments/` tree into the exported
publication directory without changing their relative paths. Do not copy only
the Layer. After a producer has placed a Layer and its revisions there, compose
with:

```bash
arc-render compose \
  --source publication/rich-source.json \
  --metadata publication/metadata.json \
  --layer publication/translation.layer.json \
  --layer publication/companion.layer.json \
  --output publication/publication.json
```

A Layer outside `publication/` is rejected even when its contents are valid.
Do not move only a Layer while leaving its referenced revisions elsewhere.

## Read Command Results

`arc-paper export-rich-document` returns the typed ARC command envelope. Check
top-level `status`, `warnings`, and `error`. On success, use:

- `data.source`: absolute `rich-source.json` path;
- `data.metadata`: absolute `metadata.json` path;
- `data.resources[]`: copied resource metadata and absolute paths;
- `data.document_digest`: frozen rich-document digest;
- `data.warnings[]`: parse and reconciliation warnings.

Successful `arc-render` commands print one flat JSON object with these exact
fields:

| Command | Fields |
| --- | --- |
| `compose` | `publication`, `publication_digest`, `source_document_digest`, `layer_count` |
| `render` | `html`, `publication_digest`, `selected_revision_digests`, `warnings` |
| `validate` | `publication`, `publication_digest`, `warnings`; plus `html` when supplied and `browser` when requested |
| `standalone-html` | `html` |

Paths identify written or checked files. Preserve render and validation
`warnings`; an empty list is the clean result.

## Validate in a Browser

Ordinary `validate` checks the publication workspace and, with `--html`, binds
the standalone reader to that publication. Add an optional local Chromium check
for reader behavior:

```bash
arc-render validate \
  --publication publication/publication.json \
  --html publication/reader.html \
  --browser \
  --browser-timeout 60
```

`--browser` requires `--html`. Use `--browser-executable <path>` only to select
a particular Chromium-family executable. Successful browser validation adds
`browser.executable` and `browser.timeout_seconds` to the flat result.

## Inline an Existing HTML Bundle

Use this separate operation when an existing local HTML entry point already
loads local CSS, JavaScript, images, fonts, media, or attachments:

```bash
arc-render standalone-html bundle/index.html standalone.html
```

It embeds local automatic resources into one offline file. External navigation
links remain links, but external automatic resources such as remote scripts,
stylesheets, or images are rejected. This does not compose or validate an ARC
publication; use `compose`, `render`, and `validate` for a native ARC reader.

## Browser Editing and Export

In an ARC reader, click a translation, companion, guide, or note body to edit
its raw Markdown. Only one draft may be active. Advanced exposes the complete
title, Markdown, preview, metadata, and revision history; closing it returns
unsaved values to the inline editor. Cancel discards the draft. Unchanged Save
is disabled and creates no revision.

A changed Save uses the revision snapshot already loaded by the browser,
writes and verifies one new immutable fragment revision, then refreshes only
the affected fragment and reading chunk. Later directory refresh incorporates
revisions written by other processes and reports forks. Directory changes,
exports, and another edit wait until the active draft is saved or cancelled.
Reading mode shows each fragment role and any non-v1 revision; editing also
shows priority and revision. Toolbar status messages clear after ten seconds.

The directory action is `New save location` until a project directory is
connected and `Change save location` afterward. Export refreshes that directory
first. Per-role Markdown export may include all latest selections or only
changes from the embedded baseline. Full-text HTML export is always complete
and available only for all-latest export from a standalone reader.

Large readers render the initial view first, then nearby and idle-time chunks.
Contents links and URL fragments materialize their target before scrolling.
Native find covers the complete document after background rendering; printing
forces remaining chunks to render.

## Printing

`arc-render` v1 has no automated PDF command. Open the standalone HTML in
Chrome and use Print / Save as PDF. Print styling hides editing/navigation
controls and stacks source and overlay blocks vertically. The PDF is a
user-created derivative: ARC does not validate, reproduce, publish, or provide
durability guarantees for it.

## Help

```bash
arc-render --help
arc-render <command> --help
arc-paper export-rich-document --help
```
