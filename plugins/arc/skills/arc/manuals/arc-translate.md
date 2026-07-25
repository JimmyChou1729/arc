# ARC Translate Manual

`arc-translate` owns reusable language detection, bilingual glossary
generation, and source-block translation. It uses `arc-paper` for verified
source parsing and keyword inventory, `arc-jobs` for durable execution, and
`arc-llm` for model calls.

## Source and project contract

The public commands accept a local HTML, Markdown, flattened single-file TeX,
or PDF source, or a paper identifier that `arc-paper` can resolve. PDF input
requires an extractable text layer and produces structured JSON rather than a
reconstructed PDF.

All three workflow commands use the same explicit project directory:

```bash
arc-translate detect-language SOURCE \
  --project-dir DIR --target-language zh-CN
arc-translate build-glossary SOURCE \
  --project-dir DIR --approx-term-count 50
arc-translate translate-blocks SOURCE \
  --project-dir DIR
```

Each command runs only its named step. `build-glossary` requires the verified
language-decision artifact in the project, and `translate-blocks` requires the
verified language decision and glossary. A missing prerequisite is a typed
failure; commands never start an earlier step implicitly.

`--approx-term-count` is an estimate, not an exact result size. `arc-paper`
discovers roughly 150 percent of that request across chapters, accepts
deduplicated underfill, and reuses or grows its lazy term inventory as needed.

## Language and glossary behavior

Translation is skipped only when detection returns a known source language
whose primary language subtag matches the target. Mixed, unknown, and different
languages enable translation.

The glossary preserves every selected source-term identity and order. For each
term it generates a preferred target-language translation and a target-language
definition. Literal `matched_sentences` supplied by `arc-paper` are source
search results used for disambiguation; they are not definitions or
explanations.

Large glossaries are divided only at complete term boundaries. Results are
validated and concatenated locally without a global reducer.

## Translation behavior

Source blocks are translated in deterministic windows without splitting a
block. Local validation preserves complete block coverage and order, plus
formula, code, link, and asset identity. Translation review belongs to
`arc-translate`; it does not review Companion learning notes.

An unsafe review can pause for supervision. The caller may discard that review
and retain the locally validated pre-review translation.

## Durable control

```bash
arc-translate status --project-dir DIR
arc-translate resume --project-dir DIR [--input JSON_OR_FILE]
arc-translate stop --project-dir DIR [--reason TEXT]
arc-translate validate --project-dir DIR
```

Resume input must match the current opaque pause descriptor. Completed child
tasks and verified artifacts replay within the same run lineage.
