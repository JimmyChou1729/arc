# ARC Document Quick Start

`arc-document` owns provider-neutral source import, deterministic parsing,
content-addressed document references, structural reads, literal text/equation
search, keyword inventory, rich-document export, and document-cache
administration. It does not identify papers, query arXiv/INSPIRE, or traverse
citations; use `arc-paper` for those academic capabilities.

Commands return the standard ARC JSON envelope. Check `status`, `warnings`,
`error`, then `data`. The cache defaults to `.arc/cache/arc-document` below the
launch directory; set `ARC_DOCUMENT_CACHE` or pass `--cache-root` consistently.

## Run ARC Document

Use `arc-document` on `PATH`, the portable Skill launcher, or the source-checkout
development fallback:

```bash
arc-document --help
<skill-dir>/scripts/arc-runtime arc-document --help
packages/arc-paper/.venv/bin/arc-document --help
```

## Import, Parse, and Export

Import one local Markdown, HTML, flattened TeX, or PDF source:

```bash
arc-document import-source source.md
arc-document parse-local source.md --validator source.pdf
```

Export the neutral `RichDocument` workspace consumed by `arc-render`:

```bash
arc-document export-rich-document source.md \
  --validator source.pdf --output-dir publication
```

The export contains `rich-source.json`, `metadata.json`, and copied resources.
Do not hand-edit resource identities after export.

Build an approximate durable keyword inventory from a local source:

```bash
arc-document extract-keywords source.md --project-dir run/keywords
```

## Frozen Cached Reads

Operations such as import/cache return a `CachedDocumentRef`. Preserve its JSON
and the cache root to make provider-free reads against the exact source digest,
parser contract, and parsed-document digest:

```bash
arc-document get-table-of-contents --document-ref '<ref JSON>'
arc-document get-section --document-ref '<ref JSON>' "Conclusion"
arc-document read-cached-source-range --document-ref '<ref JSON>' 10 30
arc-document search-full-text --document-ref '<ref JSON>' --term "phrase"
arc-document search-equations --document-ref '<ref JSON>' --term "2.1"
```

These commands never discover or download a paper. Use `arc-paper` first when
the source is identified by arXiv, DOI, INSPIRE, URL, or academic title.

## Cache Administration

List neutral derived entries, then select exact document or entry IDs. Removal
is a dry run unless `--yes` is supplied:

```bash
arc-document cache list
arc-document cache remove --entry-id '<exact entry id>'
arc-document cache remove --entry-id '<exact entry id>' --yes
```

Paper provider-response cache, academic identities, portable paper-cache
archives, and refresh remain owned by `arc-paper`.

Generic run controls are available as `arc-document status`, `stop`, and
`validate` for document workflows that publish durable runs.

## Help

```bash
arc-document --help
arc-document <command> --help
```
