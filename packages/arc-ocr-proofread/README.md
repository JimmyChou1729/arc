# arc-ocr-proofread

`arc-ocr-proofread` compares MinerU page-mapped OCR with complete PDF page
images. It uses `arc-jobs` for durable page work, `arc-llm` for bounded model
calls, and the public `arc-paper` PDF renderer.

```bash
arc-ocr-proofread --help
```

```bash
arc-ocr-proofread proofread book.md --pdf book_origin.pdf \
  --content-list book_content_list.json --project-dir project
arc-ocr-proofread status --project-dir project
arc-ocr-proofread resume --project-dir project --input review.json
arc-ocr-proofread reconcile-boundaries --project-dir project \
  --pdf book_origin.pdf
arc-ocr-proofread validate --project-dir project
```

The content list is mandatory and authoritative for page boundaries. The
package does not run OCR or infer page alignment. Every adjacent page pair is
reviewed against both full-page images; confirmed split paragraphs are joined,
while page boundaries remain structured provenance notes. The
`reconcile-boundaries` command adds this review to a verified older delivery
without repeating page proofreading. Private state lives below
`.arc/ocr-proofread/`; successful delivery is `proofread.md`,
`proofread.manifest.json`, `proofread.changes.jsonl`, and
`proofread-assets/`.

## Tests

```bash
python -m pytest packages/arc-ocr-proofread/tests
```
