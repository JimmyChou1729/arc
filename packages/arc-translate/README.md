# arc-translate

`arc-translate` provides three standalone, durable scientific-translation
steps:

```bash
arc-translate detect-language SOURCE \
  --target-language zh-CN --project-dir PROJECT
arc-translate build-glossary SOURCE \
  --approx-term-count 50 --project-dir PROJECT
arc-translate translate-blocks SOURCE --project-dir PROJECT
```

Each step is explicit. A command fails when its verified prerequisite artifact
is missing; it never runs an earlier step implicitly. `status`, `resume`,
`stop`, and `validate` operate on the project's currently selected run.

Local Markdown, HTML, flattened single-file TeX, and PDF sources are supported.
A non-path `SOURCE` is treated as a paper ID and resolved through `arc-paper`.
PDF input must have an extractable text layer.

All successful workflow outputs are immutable JSON artifacts managed by
`arc-jobs`. Translation is skipped only when language detection returns
`classification=known` and the source and target share the same primary
language subtag. Mixed, unknown, and different languages remain enabled.
