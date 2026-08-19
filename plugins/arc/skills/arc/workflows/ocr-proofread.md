# OCR Proofreading Workflow

Use this workflow only for an explicit request to proofread page-mapped OCR.
Read `manuals/arc-ocr-proofread.md` before the first command.

## Phase 1: Preflight

Require OCR Markdown, the original PDF, and MinerU content-list JSON. The
content list is the authoritative page map. If it is missing, unsafe, or
disagrees with the PDF page range, stop before any model call; never infer or
repair alignment. Use the user-supplied project directory as the project root
itself. Inside this checkout, use one stable ignored path under `local/`.

## Phase 2: Run

```bash
arc-ocr-proofread proofread <source.md> --pdf <source.pdf> \
  --content-list <content-list.json> --project-dir <project-dir> \
  --provider auto --model-tier medium --workers 30 --max-workers 200
```

Complete page images, not crops, are model inputs. Default to 30 workers; 200
is the hard ceiling. A live reduction changes the target smoothly and does not
cancel active pages. Check long runs no more often than the user requested;
when no cadence is given, hourly status is sufficient. Report completed and
failed pages, correction records, and `corrections_per_completed_page`.

Use `workers set`, `stop`, and same-project `resume` for runtime steering. Do
not kill worker processes to reduce concurrency.

## Phase 3: Main-Agent Review

The run pauses with a public request artifact when any source-typo candidate
or uncertainty exists. Inspect each requested page image and surrounding text.
Obvious errors printed in the source should be corrected, but every such
extra-source correction requires an explicit main-agent decision. Accept only
when page evidence supports the exact edit; otherwise reject and preserve the
source. Review every item exactly once and resume with the opaque `resume_key`.
Never bulk-accept model judgments.

The run then pauses for a deterministic audit of up to 10 recorded changes and
10 pages. For each change, confirm the edit matches the page image and did not
alter correct source text. For each page, compare the complete corrected
Markdown with the complete image for uncovered errors. Mark `pass` only after
inspection. Any failure prevents publication; diagnose and correct a general
package or prompt defect before rerunning.

## Phase 4: Deliver

```bash
arc-ocr-proofread validate --project-dir <project-dir>
arc-ocr-proofread get-result --project-dir <project-dir>
```

Claim completion only when validation succeeds. Deliver `proofread.md`,
`proofread.manifest.json`, `proofread.changes.jsonl`, and
`proofread-assets/`. Report OCR corrections, approved source corrections,
average corrections per page, and the 10-change/10-page audit outcome.
