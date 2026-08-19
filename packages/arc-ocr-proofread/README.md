# arc-ocr-proofread

`arc-ocr-proofread` compares MinerU page-mapped OCR with complete PDF page
images. It uses `arc-jobs` for durable page work, `arc-llm` for bounded model
calls, and the public `arc-paper` PDF renderer.

```bash
arc-ocr-proofread proofread book.md --pdf book_origin.pdf \
  --content-list book_content_list.json --project-dir project
arc-ocr-proofread status --project-dir project
arc-ocr-proofread resume --project-dir project --input review.json
arc-ocr-proofread validate --project-dir project
```

The content list is mandatory and authoritative for page boundaries. The
package does not run OCR or infer page alignment. Private state lives below
`.arc/ocr-proofread/`; successful delivery is `proofread.md`,
`proofread.manifest.json`, `proofread.changes.jsonl`, and
`proofread-assets/`.
