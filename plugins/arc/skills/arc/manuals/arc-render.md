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

Click a translation, companion, guide, or note body to replace its rendered
content with an inline raw-Markdown editor. One draft may be active at a time.
Advanced opens the full title, Markdown, preview, metadata, and history view;
closing it returns the latest unsaved values to the inline editor. Cancelling
inline editing discards the draft. The browser editor saves from its currently
loaded revision snapshot. Reading mode shows the fragment role and shows its
revision only when it is not v1; inline editing shows the role, priority, and
revision. Save is disabled while normalized content is
unchanged; the defensive no-op path uses no directory picker and creates no new
revision. A changed save writes and verifies only the
new immutable revision, then refreshes the affected fragment and reading chunk
without rescanning the fragments tree. Revisions
written by another process are incorporated, and any resulting fork is
reported, when the reader next connects to or restores the project directory.
Nested fragment directories and changed revision files are scanned with
bounded concurrency during that refresh.

The directory button reads `New save location` before a project directory is
available and `Change save location` afterwards. The reader's Export panel
refreshes a connected directory before producing any file. A role-specific
Markdown export may include all latest selections or only selections changed
from the HTML's embedded baseline. Full-text HTML export is always complete,
so its action is shown only when all latest selections are requested. It
produces another standalone interactive reader with the latest revision
histories and selections. This HTML option is disabled for a non-standalone
asset bundle. Directory changes, exports, and a
second edit are blocked until the active draft is saved or cancelled.

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
