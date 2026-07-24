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

## PDF extraction

Deterministic PDF text extraction uses the replaceable `pdftotext` adapter.
Install the `pdftotext` executable separately when PDF text extraction is
needed; `arc-paper` does not provide a Python PDF-library optional extra.
