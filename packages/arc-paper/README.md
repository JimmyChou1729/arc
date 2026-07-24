# arc-paper

`arc-paper` stores and parses research-paper sources, including PDFs.

## PDF extraction

Deterministic PDF text extraction uses the replaceable `pdftotext` adapter.
Install the `pdftotext` executable separately when PDF text extraction is
needed; `arc-paper` does not provide a Python PDF-library optional extra.
