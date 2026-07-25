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
  --project-dir local/example/translation
```

Use `arc-translate --help` and `arc-translate detect-language --help` for the
three independent stages, their prerequisites, and durable project controls.

## Tests

The default suite uses deterministic sources and fake model services:

```bash
python -m pytest packages/arc-translate/tests
```
