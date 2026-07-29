# arc-render

`arc-render` composes immutable source-bound overlay layers into a standalone
HTML reader.

## Quick Start

Compose a publication from a source reference and one or more layer files:

```bash
arc-render compose \
  --source-ref source-ref.json \
  --layer translation.layer.json
```

Render the publication to standalone HTML:

```bash
arc-render render \
  --publication publication.json \
  --html reader.html
```

Validate the publication and the generated standalone reader together before
delivery:

```bash
arc-render validate \
  --publication publication.json \
  --html reader.html
```

Use `arc-render standalone-html INPUT OUTPUT` to inline an HTML document. The
resulting reader contains its assets and needs no filesystem permission to
open. Browser editing, if used, writes new immutable fragment revisions rather
than changing an existing revision.

## Progressive Reading

Large publications become interactive after the initial reading view is
rendered. The reader then renders nearby chunks as they approach the viewport
and fills the remaining chunks one at a time during browser idle periods.
Rendered chunks remain mounted.

Contents links and URL fragments render the required chunk before scrolling,
including deep links opened directly from the filesystem. Native browser find
covers the full publication after background completion. Printing forces any
remaining chunks to render first.

## Printing

Automated PDF generation is not part of `arc-render` v1. Open the standalone
HTML in Chrome and use Print / Save as PDF when a PDF copy is needed. The print
stylesheet hides editing and navigation controls and places source and overlay
blocks vertically. That manually printed PDF is a user-side derivative, not an
ARC release artifact; ARC does not validate, reproduce, automatically publish,
or make durability guarantees for it.

## Help

```bash
arc-render --help
arc-render <command> --help
```
