# ARC Translate Quick Start

`arc-translate` owns reusable language detection, bilingual glossary
generation, block translation, and translation review. Use it when those steps
must run independently of a Companion build. The source may be a verified
local rich document or a paper identifier resolved through `arc-paper`.

## Run the Three Steps

First detect whether translation is needed:

```bash
arc-translate detect-language <source> \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --target-language <language-tag>
```

Set `<host-authority>` once per run: use `unrestricted` only when the host
explicitly reports unrestricted authority; otherwise use `unknown`. Reuse the
identical value for every resume of that run.

If the detected source language already matches the target, stop. Otherwise
build the glossary and translate all source blocks:

```bash
arc-translate build-glossary <source> \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority> \
  --approx-term-count 50

arc-translate translate-blocks <source> \
  --project-dir <project-dir> \
  --paper-cache-root <shared-paper-cache> \
  --host-authority <host-authority>
```

Each command runs only its named step and verifies its prerequisites. The term
count is approximate; deduplicated underfill is accepted. Glossary
`matched_sentences` are literal source search hits for disambiguation, never
definitions or explanations.

`--paper-cache-root` is optional. Without it, ARC uses the shared reusable
paper cache. Translation durable state lives only in
`<project-dir>/.arc/translate/`, while every successful step publishes a
human-readable `<project-dir>/translation.html` for direct delivery. No
Markdown-only result is published.

## Inspect, Resume, and Validate

```bash
arc-translate status --project-dir <project-dir>
arc-translate validate --project-dir <project-dir>
arc-translate resume --project-dir <project-dir> --input <resume-input.json> \
  --host-authority <host-authority>
arc-translate stop --project-dir <project-dir> --reason "<reason>"
```

Use the same project directory for every step. Resume input must match the
current typed pause descriptor; completed and verified work is reused in the
same run lineage.

## Help

```bash
arc-translate --help
arc-translate <command> --help
```

Use help for source requirements, provider selection, refresh behavior, and
typed failure details.
