# ARC Paper Quick Start

`arc-paper` is ARC's deterministic paper and source layer. Use it for metadata,
references, full text, cache search, and keywords. Managed ARC workflows build
on it. Commands return typed JSON; inspect status, warnings, data, and error.

Absent an override, cache lives below the current directory at
`.arc/cache/arc-paper`.
Use `cache export <entry-id> ... --output <file>` for exact `cache list` IDs or
`--all` for everything. Import merges a verified tar.gz; conflicts need `--replace-conflicts`.

## Read a Paper

Inspect only the source regions and citation relationships needed:

```bash
arc-paper get-metadata <paper-id>
arc-paper get-arxiv-table-of-contents <arxiv-id>
arc-paper get-arxiv-section <arxiv-id> "<section title or id>"
arc-paper get-references <paper-id> --enrich
arc-paper get-citers <paper-id> --limit 100 --sort mostrecent
```

The arXiv commands acquire cache-first HTML. Use `--refresh` only when the
request requires a fresh remote response.

## Search a Citation Neighborhood

```bash
arc-paper search-citers <paper-id> \
  --term "<specific phrase>" --term "<synonym>" \
  --scan-limit 1000 --limit 50
```

Matching is literal-OR after normalizing case, punctuation, and hyphens. Large
neighborhoods split the scan between the most recent and most cited records and
report `scan_complete: false`. Results identify the matching terms and fields;
recent and highly cited controls help expose keyword blind spots. Missing
matches are evidence about this neighborhood, not proof of novelty.

## Parse a Local Source

Use one HTML, Markdown, flattened TeX, or PDF as the authoritative primary.
A PDF validator checks fidelity and page evidence without replacing the text:

```bash
arc-paper parse-local <chapter.tex> --validator <book.pdf>
arc-paper parse-local <note.md> --validator <note.pdf>
arc-paper parse-local <paper.pdf>
```

Keep reconciliation and page-mapping warnings visible. A validator may be the
same document or a whole-book PDF paired with one chapter source.

## Search Materialized Full Text

Put specific multiword synonyms in one literal-OR request over verified cached
documents:

```bash
arc-paper search-cached-full-text \
  --term "<specific multiword phrase>" --term "<synonym>" \
  --limit 100 --context-lines 0
```

Broad results return `refinement_required` with exact counts and up to 50 paper
titles, not abstracts. `rg_unavailable` means install `rg`; do not replace the
operation with ad hoc physical-cache inspection.

## Read One Cached Document

Use `ArcPaperService.cache_document(source)` to obtain a
`CachedDocumentRef` for a repository `SourceArtifact` or verified
`ParsedDocument`. Its JSON records source format, digest, size, media type,
parser contract, and parsed-document digest.

```bash
arc-paper get-cached-table-of-contents --document-ref '<ref JSON>' --cache-root <root>
arc-paper get-cached-section --document-ref '<ref JSON>' --cache-root <root> "<selector>"
arc-paper read-cached-source-range --document-ref '<ref JSON>' --cache-root <root> <start> <end>
arc-paper search-cached-document --document-ref '<ref JSON>' --cache-root <root> "<query>"
```

These commands never contact providers or discover other documents. Each call
revalidates the source identity, parser contract, and parsed-document digest.
A missing or damaged derived parse may be rebuilt deterministically from
verified source bytes; missing or damaged source bytes produce a typed failure.
Text ranges are UTF-8, one-based, inclusive, and unavailable for PDFs. Keep the
logical ref; physical cache paths are private, unstable, and are not
provenance.

## Build an Approximate Keyword Inventory

```bash
arc-paper extract-keywords <source> \
  --project-dir <project-dir>/keywords \
  --host-authority <host-authority> --approx-count 50
```

Use `unrestricted` only when explicitly granted; otherwise use `unknown`, and
reuse the value on resume. Restricted/unknown host requests follow
`manuals/arc-llm.md`. The count is approximate: chapter-selected terms are
deduplicated and labeled with machine-counted occurrence frequency, without
padding. Explicit indexes receive model review. `matched_sentences` are
grounding hits, never definitions.

Keyword extraction is durable. Resume with the returned descriptor and the
same project/run identity; use `arc-jobs` for generic lifecycle operations.

## Help and Recovery

```bash
arc-paper --help
arc-paper <command> --help
```

Use command help for cache administration and exact flags. Remote failures do
not invalidate verified cache entries. Parsing and search failures are typed;
do not bypass them by reading physical cache paths.
