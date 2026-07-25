# arc-paper

`arc-paper` stores and parses research-paper sources, including PDFs.

## Cache-first arXiv documents

Deep document queries accept a bare arXiv ID, an `arXiv:` identifier, a
versioned identifier, or an arXiv URL. They fetch ar5iv HTML on the first
request and reuse verified source and parsed-document entries in the ARC user
cache thereafter:

```bash
arc-paper get-arxiv-table-of-contents 0911.3380
arc-paper get-arxiv-section 0911.3380 Introduction
arc-paper search-arxiv-full-text 0911.3380 "Hamiltonian constraint"
arc-paper search-arxiv-equations 0911.3380 'H^2'
```

These operations never fall back to PDF. Provider and parse failures are
reported with their typed error codes. Local-source import, explicit PDF
acquisition, validation, and visual review remain available through the
lower-level source APIs.

## Cached full-text search

Every source successfully processed through the public paper service,
repository-backed parser, or package workflows has a verified, content-addressed
`ParsedDocument` projection. Search the current catalog without fetching the
network:

```bash
arc-paper search-cached-full-text \
  --term "heavy field" \
  --term "massive exchange" \
  --limit 100 \
  --context-lines 0
```

Repeated terms are literal OR alternatives. Prefer several concrete multiword
synonyms in one call; broad single words often require refinement. The command
requires `rg` and returns `rg_unavailable` when ripgrep is absent. It searches
only catalog-selected, verified parsed-document payloads and never arbitrary
paths or unrelated cache files. Matching is case-insensitive unless
`--case-sensitive` is set.

Results above the requested occurrence limit return exact counts and at most
the top 50 matching paper titles in `top_paper_titles`, with no occurrences,
abstracts, summaries, or context. It never returns abstracts or summaries.
Cached display titles use deterministic section, page-line,
canonical-arXiv-ID, or local-digest fallbacks and are resolved without network
access. There is no cache list, delete, or administration command;
`search-cached-full-text` is the controlled read-only search surface.

## PDF extraction

Deterministic PDF text extraction uses the replaceable `pdftotext` adapter.
Install the `pdftotext` executable separately when PDF text extraction is
needed; missing or timed-out extraction is a typed parse failure, while a valid
PDF with no text layer keeps a warning-bearing parsed projection. Custom PDF
extractors must declare a nonempty stable contract ID. `arc-paper` does not
provide a Python PDF-library optional extra.
