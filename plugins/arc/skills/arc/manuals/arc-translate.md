# ARC Translate Quick Start

`arc-translate` owns reusable scientific language detection, bilingual
glossary generation, source-block translation, and translation review. Use it
when these steps must run independently of a Companion build. A source may be
a verified local Markdown, HTML, or flattened TeX document, or a paper
identifier resolved through `arc-paper`.

Every command returns a typed JSON envelope. Check top-level `status`,
`warnings`, and `error` before using fields under `data`.

## Run ARC Translate

Examples below assume `arc-translate` is on `PATH`. Check once with:

```bash
arc-translate --help
```

If unavailable, use the portable Skill launcher. Inside an ARC source checkout,
the package virtual environment is a direct development fallback:

```bash
<skill-dir>/scripts/arc-runtime arc-translate --help
packages/arc-paper/.venv/bin/arc-translate --help
```

Use the selected launcher in place of `arc-translate` below. Do not search
package internals for another executable.

## Run the Three Translation Steps

This concrete example translates arXiv:0911.3380 into Chinese in one stable
project directory.

### 1. Detect the Source Language

```bash
arc-translate detect-language arXiv:0911.3380 \
  --target-language zh-CN \
  --project-dir local/quasi-single-field-translation \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown

arc-translate get-result \
  --project-dir local/quasi-single-field-translation \
  --step language
```

`detect-language` returns the durable run identity at `run.id`. Read the
verified classification with `get-result`; its exact fields are:

| Meaning | JSON result path |
| --- | --- |
| Detected language | `data.result.language_tag` |
| Requested target | `data.result.target_language` |
| Translation decision | `data.result.mode` |

`data.result.mode` is `enabled` when translation should continue and `skipped`
when source and target languages already match. If it is `skipped`, stop; no
glossary or translated reader delivery is needed.

Set `--host-authority` once per run lineage. Use `unrestricted` only when the
host explicitly reports unrestricted authority; otherwise use `unknown`.
Reuse the identical value for subsequent generation and resume commands. For
`restricted` or `unknown` host requests, follow `manuals/arc-llm.md`; do not
assume a universal broker.

### 2. Build and Inspect the Glossary

```bash
arc-translate build-glossary arXiv:0911.3380 \
  --project-dir local/quasi-single-field-translation \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown \
  --approx-term-count 50

arc-translate get-result \
  --project-dir local/quasi-single-field-translation \
  --step glossary
```

The verified glossary is at `data.result.entries[]`; its requested approximate
size is `data.result.approx_count`, and its content identity is
`data.result.inventory_digest`. The target term
count is approximate and deduplicated underfill is accepted. Each entry's
`matched_sentences` are literal source search hits for disambiguation, never
definitions or explanations.

### 3. Translate and Inspect All Blocks

```bash
arc-translate translate-blocks arXiv:0911.3380 \
  --project-dir local/quasi-single-field-translation \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown

arc-translate get-result \
  --project-dir local/quasi-single-field-translation \
  --step blocks
```

Each generation command runs only its named step and verifies its selected
prerequisites. A successful block translation reports the native Layer at
`data.delivery.layer` and revision count at `data.delivery.revision_count`;
`get-result --step blocks` returns the canonical translation contract at
`data.result`, including `source_language`, `target_language`, `mode`, and
`coverage`. The Layer also appears in `artifacts[]` with role `layer`.

The delivery is normally
`<project-dir>/translation.layer.json`, with immutable Markdown revisions
under `<project-dir>/fragments/`. Language and glossary steps publish durable
prerequisites but no reader. Compose the Layer with `arc-render` to produce
standalone HTML; `arc-translate` itself does not publish a translation HTML
file. Follow `manuals/arc-render.md`: export the same source with
`arc-paper export-rich-document`, then copy both `translation.layer.json` and
the complete `fragments/` tree below that publication root before composing.
The Layer binds the exact rich source, so a different parse is rejected rather
than silently misanchoring translations.

## Status, Recovery, and Validation

```bash
arc-translate status --project-dir local/quasi-single-field-translation
arc-translate validate --project-dir local/quasi-single-field-translation
arc-translate resume \
  --project-dir local/quasi-single-field-translation \
  --paper-cache-root local/cache/arc-paper \
  --host-authority unknown
arc-translate stop --project-dir local/quasi-single-field-translation \
  --reason "operator requested stop"
```

Status reports the selected step at `data.current_step`, its run snapshot at
`data.run`, and all selected language, glossary, and block snapshots under
`data.steps`. Inspect `data.run.status`, `data.run.can_resume`,
`data.run.error`, `data.run.resume`, and the paths under
`data.run.working_state`. Use `get-result --step language|glossary|blocks`
only to read a verified successful selected result. Validation returns
`data.valid`, `data.issues`, and, for a completed block translation,
`data.delivery.layer`.

Resume the same project and selected step after a pause, interruption, failure,
or stop. Omit `--input` when no response is required; otherwise pass either
the exact ResumeInput JSON object or a file containing it. Completed and
verified work is reused within the same run lineage.

For model-correctable machine-output failures—changed term or block identities,
missing coverage, or damaged formula, link, language, or review identity—ARC
makes one fresh complete generation attempt with validation feedback. If the
fresh result remains unusable, the step pauses with both attempts and an
editable candidate; it never makes a third automatic attempt. Completed
windows are reused, and an invalid review cannot replace its valid pre-review
translation.

Provider or authority failures, prerequisite binding errors, input-budget
limits, and corrupt durable artifacts follow their typed paths and do not
consume this semantic retry. Translation quality, scientific judgment, style,
and other debatable choices belong to review; do not classify them as
machine-invalid output.

Use the same project directory for every step. Durable translation state lives
only below `<project-dir>/.arc/translate/`. Without `--paper-cache-root`, paper
data uses `.arc/cache/arc-paper` below the launch directory; keep the working
directory stable or pass the same explicit local cache to every source-reading
or resume command.

## Help

```bash
arc-translate --help
arc-translate detect-language --help
arc-translate get-result --help
arc-translate <command> --help
```

Use command help for accepted source forms, provider and model selection,
refresh behavior, project state, result selection, and exact failure flags.
