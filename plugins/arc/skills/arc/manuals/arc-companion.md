# ARC Companion Manual

`arc-companion` creates a source-anchored textbook companion with a searchable
PDF and responsive static Web reader.

## Source contract

- Authoritative source: Markdown, HTML, or single-file flattened TeX.
- Optional validator: PDF, used only for mismatch checks and page mapping.
- A missing PDF is a technical warning and does not enter book content.
- An explicit PDF mismatch or ambiguity fails before any model call.
- Relative local figures are imported into the `arc-paper` source repository
  by content digest.

## Build contract

```bash
arc-companion build SOURCE --project-dir DIR [options] --json
```

Options:

- `--pdf PATH`: local PDF validator.
- `--pdf fetch`: fetch the PDF validator for a remote paper ID.
- `--target-language TAG`: generated guide/translation language; default
  `zh-CN`.
- `--user-intent TEXT`: optional reading goal; empty uses the fixed neutral
  textbook intent.
- `--approx-term-count N`: approximate glossary size; default 50, range 1–200.
- `--provider NAME`, `--model NAME`: explicit generation recipe. An exact
  model requires an explicit provider.
- `--workers N`: bounded chapter concurrency, 1 through 24.
- `--refresh`: refresh remote source acquisition.

The build delegates language detection, bilingual glossary generation, and
translation review to `arc-translate`. After the glossary/evidence barrier,
each chapter's translation and Companion guide run in parallel. Only glossary
entries whose source term occurs in the chapter are included in that chapter's
prompts. Translation, when enabled, covers every block exactly once. Learning
units are selective and anchored to source block IDs.

The term count is approximate. Chapter extraction deliberately has headroom,
and deduplicated underfill is accepted without padding or count-based retries.
Literal matched sentences used to ground glossary generation are search
results, not definitions.

## Durable control

```bash
arc-companion status --project-dir DIR --json
arc-companion resume --project-dir DIR [--input JSON_OR_FILE] --json
arc-companion stop --project-dir DIR [--reason TEXT] --json
```

Resume input must match the opaque key and closed contract in the current
pause descriptor. Stop pauses the current attempt; `resume` continues this
same run. Completed child LLM tasks and accepted chapter artifacts
replay within the same run lineage; changing workers does not invalidate them.

## Rendering

```bash
arc-companion render --project-dir DIR --format all|pdf|web --json
arc-companion validate --project-dir DIR --json
```

Rendering loads only the immutable `AcceptedBook`; it does not load an LLM
runtime. Wide Web layouts show source/translation and textbook notes in
parallel; narrow layouts interleave source, translation, then notes by anchor.
PDF uses the same anchor order. PDF validation checks metadata, searchable
text, embedded fonts, and a page raster in temporary directories that are
always removed.

Successful output is immutable under `releases/<release-id>/`; `current.json`
is replaced only after complete validation. There is no v1 `package`, `gc`, or
`render-web` command.
