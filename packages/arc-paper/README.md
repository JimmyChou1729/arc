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
access. `search-cached-full-text` remains the controlled read-only search
surface and is separate from the explicit administration commands below.

## Cache administration

Inspect paper, local-source, and opaque legacy entries without modifying their
read timestamps:

```bash
arc-paper cache list
arc-paper cache list --since 1d
arc-paper cache list --id arXiv:0911.3380
```

`--since` is a rolling UTC window from the last successful write or refresh. It
accepts one positive integer followed by `s`, `m`, `h`, `d`, or `w`; `1d` is
exactly 86,400 seconds. Results are newest first. Ordinary cache reads do not
change this time. Legacy entries use file modification time when possible and
may be listed under an exact opaque entry ID.

Removal always requires an exact paper or entry ID. Without `--yes`, the
command only previews the selected entries:

```bash
arc-paper cache remove --id arXiv:0911.3380
arc-paper cache remove --id arXiv:0911.3380 --yes
arc-paper cache remove --entry-id local:markdown:<sha256> --yes
```

`--yes` physically deletes the selected request mappings, full-text locators,
source objects, and parsed-document objects. It does not perform a reference
scan or general garbage collection, so deleting a shared content-addressed
source may temporarily invalidate another request mapping. A later remote read
repairs that mapping by fetching the source again. ARC cannot reacquire a local
source automatically; keep the original local file before deleting its cache
entry, or the cached copy is not recoverable.

Update is paper-only and uses a fixed refresh set:

```bash
arc-paper cache update --id arXiv:0911.3380
```

It independently refreshes the INSPIRE record, the `mostrecent` and
`mostcited` citer sets at limit 1000, ar5iv HTML plus parsing, and arXiv PDF
plus parsing. A failed component is reported while the remaining components
continue; previously published mappings are not proactively removed.

## PDF extraction

Deterministic PDF text extraction uses the replaceable `pdftotext` adapter.
Install the `pdftotext` executable separately when PDF text extraction is
needed; missing or timed-out extraction is a typed parse failure, while a valid
PDF with no text layer keeps a warning-bearing parsed projection. Custom PDF
extractors must declare a nonempty stable contract ID. `arc-paper` does not
provide a Python PDF-library optional extra.
