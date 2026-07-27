# arc-translate

`arc-translate` owns reusable scientific language detection, LLM-reviewed
bilingual glossary generation, source-block translation, and translation
review over verified `arc-paper` documents. It uses `arc-jobs` for durable
execution and `arc-llm` for model calls; Companion rendering is outside this
package.

## Quick start

Detect whether a verified source needs translation:

```bash
arc-translate detect-language note.md \
  --target-language zh-CN \
  --project-dir local/example/translation \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority>
```

Use `arc-translate --help` and `arc-translate detect-language --help` for the
three independent stages, their prerequisites, and durable project controls.

`--paper-cache-root` is optional and otherwise resolves to ARC's shared paper
cache. Durable state is project-local under `<project-dir>/.arc/translate/`.
Each successful step publishes the human-readable
`<project-dir>/translation.html`; no Markdown-only output is delivered.

Generation and resume commands accept `--host-authority`. Set
`<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value when resuming. The setting is execution-only and is not
recorded in a translation request or result.

A failed step is the latest failed attempt, not a permanent project terminal
state. Status exposes `can_resume`, `recovery_epoch`, and stable `working/`
paths. Glossary candidates contain only term IDs plus translated content, and
translation candidates contain only block IDs plus translated text; source
identity is attached locally. An agent may correct a candidate and resume
without another provider call. Deleting an exhausted retry candidate does not
grant a third automatic generation attempt.

When a model response is structurally valid JSON but violates a
machine-checkable language, term, block, formula, link, or review identity or
coverage contract, ARC makes one fresh full-generation attempt with validation
feedback. A second unusable response pauses with both attempts and an editable
candidate preserved. Completed windows and a valid pre-review translation stay
available. Provider, authority, binding, input-budget, and corrupt-artifact
failures are not semantic-output retries. Translation quality and scientific
judgment are reviewer concerns, not program-invalid output.

## Tests

The default suite uses deterministic sources and fake model services:

```bash
python -m pytest packages/arc-translate/tests
```
