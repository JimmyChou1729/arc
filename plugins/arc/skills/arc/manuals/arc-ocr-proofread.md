# ARC OCR Proofreading Quick Start

`arc-ocr-proofread` compares MinerU page-mapped Markdown with complete PDF page
images and records exact corrections in a durable ARC project.

## Start

```bash
arc-ocr-proofread --help
arc-ocr-proofread proofread source.md --pdf source.pdf \
  --content-list source_content_list.json --project-dir project \
  --workers 30 --max-workers 200
```

If the bare command is unavailable, use the portable Skill runtime:

```bash
<skill-dir>/scripts/arc-runtime arc-ocr-proofread --help
```

Markdown, PDF, and MinerU content list are all required. The package does not
perform OCR or infer page alignment. It validates page indices before any
provider call. Private durable state is under
`<project-dir>/.arc/ocr-proofread/`.

## Observe and Control

```bash
arc-ocr-proofread status --project-dir project
arc-ocr-proofread workers get --project-dir project
arc-ocr-proofread workers set --project-dir project --workers 10
arc-ocr-proofread stop --project-dir project --reason "user requested stop"
```

The default is 30 workers and the configured maximum cannot exceed 200.
Lowering the target does not cancel active work. Status includes completed and
failed pages, correction records, and average correction records per completed
page. Hourly checks are normally enough; do not busy-poll.

## Review and Resume

A pause reports `resume.request_artifact` and `resume.resume_key`. Read that
artifact, inspect every referenced full page, and create one JSON input covering
every requested item. Source-typo candidates are never applied automatically.
Accept an obvious printed typo only after visual confirmation; reject uncertain
cases. An accepted uncertainty must provide an exact edit.

The next pause requests deterministic samples of up to 10 changes and 10 whole
pages. Inspect every sample and submit `pass` or `fail` for every listed ID:

```bash
arc-ocr-proofread resume --project-dir project --input decisions.json
```

Resume uses the same durable run. A failed audit prevents delivery.

## Results

```bash
arc-ocr-proofread validate --project-dir project
arc-ocr-proofread get-result --project-dir project
```

Successful output is `proofread.md`, `proofread.manifest.json`,
`proofread.changes.jsonl`, and `proofread-assets/`. The manifest distinguishes
OCR corrections from main-agent-approved source corrections and reports
corrections per page.

## Help

```bash
arc-ocr-proofread --help
arc-ocr-proofread <command> --help
```
