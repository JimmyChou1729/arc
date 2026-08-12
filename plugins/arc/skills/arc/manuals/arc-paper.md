# ARC Paper Quick Start

`arc-paper` is ARC's deterministic paper-reading layer. Use it to identify a
paper, inspect its structure, read sections, search prose or equations, follow
citations, parse local sources, and build keyword inventories. Commands return
a typed JSON envelope. Always check `status`, top-level `warnings`, `error`, and
the relevant fields under `data`; an empty result is not enough to diagnose why
nothing was found.

The paper cache defaults to `.arc/cache/arc-paper` below the current directory.
Use the same working directory or set `ARC_PAPER_CACHE` consistently across
related commands. Inside an ARC source checkout, use an ignored location such
as `local/cache/arc-paper`.

## Start Here: Read One Paper

These commands cover the usual reading loop for arXiv:0911.3380:

```bash
# Identify the paper.
arc-paper get-metadata arXiv:0911.3380

# See its structure.
arc-paper get-table-of-contents \
  --reference 0911.3380 --source-format html

# Find a term and its surrounding prose.
arc-paper search-full-text \
  --reference 0911.3380 --source-format html \
  --term "isocurvaton" --context-lines 1

# Read the complete conclusion, not only a search snippet.
arc-paper get-section \
  --reference 0911.3380 --source-format html "Conclusion"

# Read printed equation 2.30 with nearby PDF layout text.
arc-paper search-equations \
  --reference 0911.3380 --source-format pdf \
  --term "2.30" --context-lines 8
```

Use these result paths first:

| Task | Result to inspect |
| --- | --- |
| Metadata | `data.title`, `data.authors`, `data.abstract`, `data.identifiers` |
| Table of contents | `data.entries[]` (`section_id`, `title`, `ordinal`) |
| Full-text search | `data.occurrences[]` (`context`, `title`, `matched_terms`) |
| Complete section | `data.title`, `data.section_id`, and `data.text` |
| Equation search | `data.matches[]` (`source_label`, `normalized_tex`, `source_excerpt`) |

Every structural read also returns the exact parsed source at
`data.source.document`. Searches return it at
`data.documents[].source.document`. Keep that object when later work must read
the identical cached representation.

## Choose a Target and Representation

For ordinary reading, use `--reference`. It accepts one exact arXiv ID, DOI,
INSPIRE ID, URL, or exact cached title. Resolution is cache-first and may
acquire a missing source. For a frozen, provider-free read, pass a previously
returned document object as JSON:

```bash
arc-paper get-section \
  --document-ref '<data.source.document JSON>' \
  --cache-root <same-cache-root> "<section id or title>"
```

`--document-ref` never discovers another paper or contacts a provider. It
verifies the source digest, parser contract, and parsed-document digest. A
damaged derived parse can be rebuilt from verified source bytes; missing or
damaged source bytes produce a typed failure. Physical cache paths are private,
unstable implementation details and are not provenance.

For references, ARC otherwise uses the first parseable representation already
recorded for that identity. Choose explicitly when the task depends on format:

- Use `--source-format html`, `--source-format markdown`, or
  `--source-format tex` for headings and prose.
- Use `--source-format pdf` for printed equation numbers and page/layout
  evidence.
- Do not assume different representations have identical headings or equation
  labels. Confirm the returned format and digests.

`--refresh` applies only to reference targets. Use it only when fresh upstream
data is required; an exact document target is immutable.

## Search the Right Surface

| Goal | Command | Search surface |
| --- | --- | --- |
| Discover papers | `search-metadata` | Titles, abstracts, authors, identifiers |
| Find text in paper bodies | `search-full-text` | Selected documents, or cached corpus without targets |
| Find equations | `search-equations` | Labels, normalized math, nearby equation text |
| Filter direct citers | `search-citers` | Titles and abstracts of papers citing one paper |

### Search Full Text

Search one or more explicit targets when the intended papers are known:

```bash
arc-paper search-full-text \
  --reference 0911.3380 \
  --reference "doi:<doi>" \
  --term "specific multiword phrase" \
  --term "alternate phrase" \
  --limit 100 --context-lines 1
```

Repeated `--term` values form literal OR: a match for any term is returned.
They are not regular expressions. Matching ignores case unless
`--case-sensitive` is set. Prefer specific multiword alternatives, then inspect
`matched_terms`, `context`, location, section or page title, and source digest.

Mixed `--reference` and `--document-ref` targets are allowed and retain their
command-line order. Duplicate parsed content is searched once. If at least one
target resolves, failures for other targets appear in `data.failures`; if none
resolve, the operation fails.

Omit targets to search every currently materialized cached document:

```bash
arc-paper search-full-text \
  --term "specific multiword phrase" \
  --term "alternate phrase" \
  --limit 100 --context-lines 0
```

A broad corpus query may return `mode: "refinement_required"`, exact counts,
and up to 50 paper titles rather than occurrences. Refine the terms or select
explicit targets. `rg_unavailable` means the required `rg` executable is
missing; do not replace this operation with physical-cache inspection.

## Read Sections and Equations

`get-table-of-contents` returns section IDs, titles, levels, zero-based
ordinals, and page bounds when the selected representation provides them.
`get-section` accepts an unambiguous title or section ID. If titles are
ambiguous, use the ordinal reported by the table of contents:

```bash
arc-paper get-section \
  --reference 0911.3380 --source-format html --ordinal 10
```

`search-equations` uses the same literal OR and ranks an exact source
label before label substrings, normalized math, and nearby context. For labeled
PDF matches, inspect `page_candidates` and the layout-preserving
`source_excerpt`. PDF extraction is approximate, so retain its warning and
source digest with any derived claim.

If a printed equation label is missing from results, compare representations
before diagnosing ARC:

```bash
arc-paper search-equations \
  --reference <paper-id> --source-format html --term "<label>"

arc-paper search-equations \
  --reference <paper-id> --source-format pdf \
  --term "<label>" --context-lines 8
```

HTML converters can omit labels, merge equation rows, or number them
differently. A PDF-only match therefore indicates a representation difference,
not by itself an ARC parser bug. Report an `arc-paper` parser bug only when the
selected raw representation visibly contains the label or math that its parsed
result omitted; include format, source digest, search term, and the smallest
reproducing command.

## Find Papers and Follow Citations

Use metadata search when the identifier is not yet known, then read normalized
metadata by exact ID:

```bash
arc-paper search-metadata "quasi-single field inflation" --limit 20
arc-paper get-metadata arXiv:0911.3380
```

Inspect a paper's citation neighborhood with exact metadata operations:

```bash
arc-paper get-references arXiv:0911.3380 --enrich
arc-paper get-citers arXiv:0911.3380 --limit 100 --sort mostrecent
```

### Search a Citation Neighborhood

Use specific alternatives to shortlist direct citers:

```bash
arc-paper search-citers arXiv:0911.3380 \
  --term "specific phrase" --term "synonym" \
  --scan-limit 1000 --limit 50
```

Matching is literal OR after normalizing case, punctuation, and hyphens. Large
neighborhoods split the scan between most recent and most cited records and
report `scan_complete: false`. Inspect matching fields and control samples for
keyword blind spots. No match is evidence about the scanned neighborhood, not
proof of novelty.

## Parse a Local Source

Use one HTML, Markdown, flattened TeX, or PDF as the authoritative primary. A
validator checks fidelity and page evidence without replacing primary text:

```bash
arc-paper parse-local <chapter.tex> --validator <book.pdf>
arc-paper parse-local <note.md> --validator <note.pdf>
arc-paper parse-local <paper.pdf>
```

Keep reconciliation and page-mapping warnings visible. A validator may be the
same document or a whole-book PDF paired with one chapter source. Use
`arc-paper parse-local --help` for explicit formats and validation policies.

For an exact cached UTF-8 text source, `read-cached-source-range` reads one-based
inclusive lines without fetching. It is unavailable for PDF sources:

```bash
arc-paper read-cached-source-range \
  --document-ref '<document JSON>' \
  --cache-root <same-cache-root> <start-line> <end-line>
```

Advanced Markdown/PDF hierarchy reconciliation is available through
`reconstruct-cached-structure --help`.

## Build an Approximate Keyword Inventory

```bash
arc-paper extract-keywords <source> \
  --project-dir <project-dir>/keywords \
  --host-authority <host-authority> --approx-count 50
```

Use `unrestricted` only when explicitly granted; otherwise use `unknown`, and
reuse the same value on resume. Restricted or unknown requests follow
`manuals/arc-llm.md`. The target count is approximate: selected terms are
deduplicated and labeled with machine-counted occurrence frequency, without
padding. Explicit indexes receive model review. `matched_sentences` are
grounding hits, never definitions.

Keyword extraction is durable. Resume with the returned descriptor and the
same project/run identity; use `arc-jobs` for generic lifecycle operations.

## Help, Cache, and Recovery

```bash
arc-paper --help
arc-paper <command> --help
arc-paper cache --help
```

Use command help for reference acquisition, cache administration, exact flags,
and advanced operations. Remote failures do not invalidate verified cache
entries. Typed parse or search errors should be investigated at their stated
source or contract boundary; do not bypass verification by reading or editing
physical cache paths.
