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

## Tests

The default suite uses deterministic sources and fake model services:

```bash
python -m pytest packages/arc-translate/tests
```
