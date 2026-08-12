# arc-render

`arc-render` owns ARC's immutable Markdown overlay contracts and standalone
reader delivery. A publication contains its frozen rich source document and
refers to immutable fragment revision and resource files, so it does not depend
on an unrecorded caller-supplied source.

## Quick start

When `arc-paper` and `arc-render` are installed on `PATH`, build a source-only
reader with:

```bash
arc-paper export-rich-document note.md --output-dir publication

arc-render compose \
  --source publication/rich-source.json \
  --metadata publication/metadata.json \
  --output publication/publication.json

arc-render render \
  --publication publication/publication.json \
  --html publication/reader.html

arc-render validate \
  --publication publication/publication.json \
  --html publication/reader.html
```

`export-rich-document` accepts local Markdown, HTML, or flattened single-file
TeX. Add `--validator note.pdf` when a PDF should validate fidelity or page
mapping. It creates the rich source, metadata, and verified resource tree; the
caller should not hand-copy resources or construct the metadata resource array.
The output directory must be new or empty.

A publication with no overlay Layers is valid. To include existing Layers,
repeat `--layer publication/<layer>.json` on `compose` in publication order.
Each Layer and every fragment revision it references must already be below the
directory containing the output `publication.json`; moving only a Layer is not
enough.

If bare commands are unavailable in an installed ARC Skill, use its runtime:

```bash
<skill-dir>/scripts/arc-runtime arc-paper export-rich-document --help
<skill-dir>/scripts/arc-runtime arc-render --help
```

From this source checkout, the development fallback is:

```bash
packages/arc-paper/.venv/bin/arc-paper export-rich-document --help
packages/arc-paper/.venv/bin/arc-render --help
```

`arc-paper export-rich-document` prints an `arc.command_result.v2` envelope.
Check top-level `status`, `warnings`, and `error`; successful paths and identity
are at `data.source`, `data.metadata`, `data.resources[]`, and
`data.document_digest`, with parse warnings at `data.warnings[]`.

Successful `arc-render` commands print flat JSON objects with these exact
fields:

| Command | Result fields |
| --- | --- |
| `compose` | `publication`, `publication_digest`, `source_document_digest`, `layer_count` |
| `render` | `html`, `publication_digest`, `selected_revision_digests`, `warnings` |
| `validate` | `publication`, `publication_digest`, `warnings`, plus `html` when supplied and `browser` when requested |
| `standalone-html` | `html` |

Ordinary validation checks the publication and, when supplied, its rendered
HTML. `--browser` requires `--html` and adds a local Chromium behavior check.
`standalone-html` is a separate utility for embedding the local assets of an
existing HTML bundle; it does not compose or validate an ARC publication.

The v1 fragment format uses strict JSON front matter between ARC-specific
delimiters. YAML is not accepted. Fragment priorities are positive integers;
block and section are the only anchor kinds. A publication with no overlay
layers is valid.

The initial public API includes:

- frozen source, anchor, fragment revision, layer, and publication value
  objects;
- exact JSON document codecs;
- canonical fragment semantic digests and JSON-front-matter Markdown codecs;
- revision resolution with diagnostics for malformed files, dangling
  revisions, and forks.

New immutable revisions are stored as
`fragments/revision-<number>-<semantic-digest>.md`. The validated JSON front
matter owns fragment identity; directory names below `fragments/` are opaque
storage organization and are never interpreted as semantic IDs.

Standalone HTML and command-line delivery are built on these contracts. For
large publications, the reader renders an initial reading view first, loads
nearby chunks on demand, and completes the remaining chunks during browser idle
time. Contents links and URL fragments render their target before scrolling;
printing renders every remaining chunk before the browser creates its preview.
The reader never unloads a rendered chunk.

Clicking a translation, companion, guide, or note body opens its raw Markdown
inline. The inline controls save or discard the single active draft; Advanced
opens the complete title, Markdown, preview, metadata, and history editor while
preserving the same unsaved values. Browser editing saves from the revision
snapshot already loaded in the reader. Reading mode shows the fragment role and
shows its revision only when it is not v1; inline editing shows the role,
priority, and revision. Save remains disabled while normalized
content is unchanged, and the defensive no-op path chooses no directory and
creates no revision. A changed save
appends and verifies one immutable revision file, then updates only the affected
fragment and render chunk; it does not rescan the fragments tree.
Files added by another process are incorporated, with fork diagnostics when
needed, the next time the reader connects to or restores the project directory.
That refresh enumerates nested fragment directories in bounded concurrent
batches and reads changed files with bounded concurrency.
Toolbar status messages clear automatically after ten seconds.

The browser toolbar calls the directory action `New save location` until a
project directory is available and `Change save location` afterwards. Export
first refreshes that connected directory. Per-role Markdown can contain either
all currently selected revisions or only selections changed from the reader's
embedded baseline. Full-text HTML export always contains the complete latest
publication and remains a standalone interactive reader, so its action is shown
only for the all-latest scope. It is available only from a standalone reader
whose assets are already embedded. Directory changes,
exports, and another edit wait until the active draft is saved or cancelled.

For now, a PDF copy may be made manually with Chrome's Print / Save as PDF
command. Such a PDF is a user-side derivative, not an ARC release artifact,
and ARC does not validate, reproduce, or automatically publish it.

Command help is available without opening the manual:

```bash
arc-render --help
arc-render <command> --help
```

Run the package tests with:

```bash
python -m pytest packages/arc-render/tests
```
