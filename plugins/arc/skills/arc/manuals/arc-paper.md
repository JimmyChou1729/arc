# ARC Paper Quick Start

`arc-paper` is ARC's deterministic paper and source layer. Use it for paper
metadata, references and citers, arXiv full text, local rich documents,
cache-wide text discovery, and approximate keyword inventories. Use a managed
workflow when the requested result is a domain, research idea, note check, or
Companion rather than paper data alone.

Commands return one typed JSON result. Read its status, warnings, data, and
error instead of relying only on the process exit code.

## Read a Paper

Start with metadata, then inspect only the source regions and citation
relationships needed for the task:

```bash
arc-paper get-metadata <paper-id>
arc-paper get-arxiv-table-of-contents <arxiv-id>
arc-paper get-arxiv-section <arxiv-id> "<section title or id>"
arc-paper get-references <paper-id> --enrich
arc-paper get-citers <paper-id> --limit 100 --sort mostrecent
```

The arXiv document commands acquire and parse cache-first HTML when needed.
Use `--refresh` only when a fresh remote response is part of the request.

## Parse a Local Source

Use one HTML, Markdown, flattened single-file TeX, or PDF as the authoritative
primary. A PDF validator checks fidelity and supplies page evidence; it never
overwrites primary text or TeX.

```bash
arc-paper parse-local <chapter.tex> --validator <book.pdf>
arc-paper parse-local <note.md> --validator <note.pdf>
arc-paper parse-local <paper.pdf>
```

This covers both a same-document validator and the common case of one chapter
TeX file paired with the whole-book PDF. Keep reconciliation and page-mapping
warnings visible in downstream work.

## Search Materialized Full Text

Search only current verified documents already in the ARC cache. Put several
specific multiword synonyms or spellings in one literal-OR request:

```bash
arc-paper search-cached-full-text \
  --term "<specific multiword phrase>" \
  --term "<synonym or alternate spelling>" \
  --limit 100 \
  --context-lines 0
```

Broad results return `refinement_required` with exact counts and up to 50
paper titles, not abstracts. The command requires ripgrep; `rg_unavailable`
means install `rg` rather than searching arbitrary cache files.

## Build an Approximate Keyword Inventory

```bash
arc-paper extract-keywords <source> \
  --project-dir <project-dir>/keywords \
  --host-authority <host-authority> \
  --approx-count 50
```

Use `unrestricted` for `<host-authority>` only when the host explicitly
reports unrestricted authority; otherwise use `unknown`. Reuse that value if
the durable keyword run is resumed. For `restricted` or `unknown` host
requests, follow `manuals/arc-llm.md`; do not assume a universal broker.

The count is an estimate. Terms are selected chapter by chapter for relevance,
deduplicated, and then labeled with machine-counted occurrence frequency.
Underfill does not cause padding or retries. Explicit indexes are reviewed by
the model before acceptance. `matched_sentences` are literal source search
hits used for grounding, never definitions.

Keyword extraction is durable. If it pauses, use the returned resume
descriptor with the same project/run identity; use `arc-jobs` for generic
status, validation, or stop operations.

## Help and Recovery

For identity utilities, source import, arXiv text/equation search, cache
administration, exact flags, and typed failures, use:

```bash
arc-paper --help
arc-paper <command> --help
```

Remote failures do not invalidate already verified cache entries. Parsing and
search failures are typed; do not bypass them by reading physical cache paths.
