# ARC Paper Quick Start

`arc-paper` is ARC's deterministic paper-reading layer. Use it to resolve a
paper, choose a representation, inspect structure, read sections, search text
or equations, inspect citations, and build keyword inventories. Commands return
typed JSON. Read `status`, `data`, `warnings`, and `error`; do not infer success
from an empty match list alone.

Unless overridden, the cache is `.arc/cache/arc-paper` below the current
directory. In the ARC checkout, use an ignored cache such as
`local/cache/arc-paper` or set `ARC_PAPER_CACHE` consistently for all commands.

## Choose a Target

Reading and focused-search commands accept either target form:

- `--reference <value>` resolves one exact arXiv ID, DOI, INSPIRE ID, URL, or
  exact cached title. It is cache-first and may acquire a missing source.
- `--document-ref '<JSON>'` reopens one immutable `CachedDocumentRef`. It never
  discovers another paper or contacts a provider.

Repeat either flag on search commands to search several papers. Mixed targets
keep command-line order. Duplicate parsed content is searched once, and the
result records all corresponding target indexes. A failed target is returned
as a typed per-target failure when another target succeeds; the operation fails
only when no target resolves.

For a reference, the default is the first parseable representation already
recorded for that reference. If none is cached, ARC acquires one. Use
`--source-format html|markdown|tex|pdf` when representation matters:

- Prefer HTML, Markdown, or TeX for headings and ordinary prose search.
- Prefer PDF for printed equation numbering and page/layout evidence.
- Do not assume two representations have identical structure or equation
  labels. The returned source format and digests say what ARC actually read.

`--refresh` applies only to reference targets. Use it only when the request
requires a fresh remote response. Exact document targets are already immutable.

## Read a Paper by Reference

This sequence performs the common structure, word, section, and equation tasks
for arXiv:0911.3380:

```bash
arc-paper get-table-of-contents \
  --reference 0911.3380 --source-format html

arc-paper search-full-text \
  --reference 0911.3380 --source-format html \
  --term "isocurvaton" --context-lines 1

arc-paper get-section \
  --reference 0911.3380 --source-format html "Conclusion"

arc-paper search-equations \
  --reference 0911.3380 --source-format pdf \
  --term "2.30" --context-lines 8
```

The table of contents returns stable section IDs, titles, levels, ordinals, and
available page bounds. `get-section` accepts a section ID or unambiguous title;
use `--ordinal <zero-based-index>` when a title is ambiguous. Read the entire
selected section from `data.text`, not merely a search snippet.

`search-equations` searches literal alternatives across source labels,
normalized math, and nearby text. Exact label matches rank before label
substrings and content matches. For PDFs, labeled matches include
`page_candidates` and a layout-preserving `source_excerpt`; extraction is
approximate, so keep the warning and source digest with any claim.

## Diagnose a Missing Equation

An absent equation label can originate in the selected source representation,
not in ARC. Compare representations before diagnosing the parser:

```bash
arc-paper search-equations \
  --reference 0911.3380 --source-format html \
  --term "2.29" --term "2.30"

arc-paper search-equations \
  --reference 0911.3380 --source-format pdf \
  --term "2.29" --term "2.30" --context-lines 8
```

If PDF finds the printed labels but HTML does not, inspect the returned source
identity and warning. Converter-produced HTML may omit labels, merge equation
rows, or number them differently. ARC does not invent a label absent from the
source because doing so would create false provenance. If the raw representation
contains a clear label but the parsed span lacks it, report the source digest,
format, label, and smallest reproducing command as an `arc-paper` parser bug.

For 0911.3380, the cached ar5iv HTML has no literal `(2.29)` or `(2.30)` tags;
their mathematical nodes are folded into the following equation group. The PDF
contains both printed labels. This is an upstream HTML-conversion defect, while
PDF equation search is the reliable route for these equations.

## Search Full Text

With targets, `search-full-text` returns occurrence-level matches in only those
documents. Repeated `--term` values are one literal-OR query, so alternatives
belong in one command:

```bash
arc-paper search-full-text \
  --reference 0911.3380 --reference "doi:<doi>" \
  --term "specific multiword phrase" --term "alternate phrase" \
  --limit 100 --context-lines 1
```

Terms are literal, not regular expressions. Matching is case-insensitive unless
`--case-sensitive` is set. Prefer specific multiword alternatives; inspect
`matched_terms`, location, section/page title, line, column, and source digest.

With no target, the same command searches the materialized cached corpus:

```bash
arc-paper search-full-text \
  --term "specific multiword phrase" --term "alternate phrase" \
  --limit 100 --context-lines 0
```

Broad corpus results may return `refinement_required` with exact counts and up
to 50 paper titles, not abstracts. Refine the terms or select explicit
targets. `rg_unavailable` means install `rg`; do not inspect private physical
cache paths or silently substitute a search with different semantics.

## Read an Exact Cached Document

`ArcPaperService.cache_document(source)` returns a `CachedDocumentRef` for a
repository `SourceArtifact` or verified `ParsedDocument`. Its JSON commits to
source format, digest, size, media type, parser contract, and parsed-document
digest.

```bash
arc-paper get-table-of-contents \
  --document-ref '<ref JSON>' --cache-root <root>

arc-paper get-section \
  --document-ref '<ref JSON>' --cache-root <root> "<selector>"

arc-paper search-full-text \
  --document-ref '<ref JSON>' --cache-root <root> --term "<term>"

arc-paper search-equations \
  --document-ref '<ref JSON>' --cache-root <root> --term "<label or math>"

arc-paper read-cached-source-range \
  --document-ref '<ref JSON>' --cache-root <root> <start> <end>
```

Each call verifies the source identity, parser contract, and parsed-document
digest. A missing or damaged derived parse may be rebuilt from verified source
bytes. Missing or damaged source bytes produce a typed failure. Text ranges are
UTF-8, one-based, inclusive, and unavailable for PDFs. Keep logical references;
physical cache paths are private, unstable, and are not provenance.

For Markdown whose hierarchy must follow an independently cached PDF outline,
run `reconstruct-cached-structure`, then pass its exact `--structure-ref` to
`get-table-of-contents` or `get-section` with the same `--document-ref`.

## Inspect Metadata and Citations

```bash
arc-paper get-metadata <paper-id>
arc-paper get-references <paper-id> --enrich
arc-paper get-citers <paper-id> --limit 100 --sort mostrecent
```

## Search a Citation Neighborhood

```bash
arc-paper search-citers <paper-id> \
  --term "specific phrase" --term "synonym" \
  --scan-limit 1000 --limit 50
```

`search-citers` applies literal-OR matching after normalizing case, punctuation,
and hyphens. Large neighborhoods split the scan between most recent and most
cited records and report `scan_complete: false`. Matching fields and control
samples expose keyword blind spots. No match is evidence about the scanned
neighborhood, not proof of novelty.

## Parse a Local Source

Use one HTML, Markdown, flattened TeX, or PDF as the authoritative primary. A
PDF validator checks fidelity and page evidence without replacing primary text:

```bash
arc-paper parse-local <chapter.tex> --validator <book.pdf>
arc-paper parse-local <note.md> --validator <note.pdf>
arc-paper parse-local <paper.pdf>
```

Keep reconciliation and page-mapping warnings visible. A validator may be the
same document or a whole-book PDF paired with one chapter source.

## Build an Approximate Keyword Inventory

```bash
arc-paper extract-keywords <source> \
  --project-dir <project-dir>/keywords \
  --host-authority <host-authority> --approx-count 50
```

Use `unrestricted` only when explicitly granted; otherwise use `unknown`, and
reuse it on resume. Restricted or unknown requests follow `manuals/arc-llm.md`.
The count is approximate: selected terms are deduplicated and labeled with
machine-counted occurrence frequency, without padding. Explicit indexes receive
model review. `matched_sentences` are grounding hits, never definitions.

Keyword extraction is durable. Resume with the returned descriptor and the
same project/run identity; use `arc-jobs` for generic lifecycle operations.

## Help, Cache, and Recovery

Use `arc-paper cache --help` for cache administration: list, export, import,
remove, and update syntax.
Export exact IDs reported by cache list, or explicitly export all. Imports
merge verified archives; replacing conflicts must be requested explicitly.

```bash
arc-paper --help
arc-paper <command> --help
arc-paper cache --help
```

Remote failures do not invalidate verified cache entries. Typed parse or search
errors should be investigated at their stated source/contract boundary. Do not
bypass verification by reading or editing physical cache files.
