# arc-companion

`arc-companion` owns source-anchored chapter-guide orchestration,
deterministic joining of translation and guide lanes, rendering, release
publication, and validation. It consumes verified documents from `arc-paper`
and reusable language, glossary, and translation results from `arc-translate`.
Its public build API is the split translation/guide workflow:
`CompanionBuildRequest`, `CompanionGenerationRecipe`, and
`CompanionBuildHandler` are the sole durable build lineage. Published content
uses the `arc.companion.accepted_book.v2` delivery contract and embeds the
current RichDocument v2 inline-span payloads directly.

## Quick start

Build a companion from a rich source or paper identifier:

```bash
arc-companion build note.md \
  --project-dir local/example/companion \
  --target-language zh-CN \
  --user-intent "Explain the main argument and its assumptions."
```

Use `arc-companion --help` and `arc-companion build --help` for supported
sources, optional PDF validation, durable controls, rendering, and release
validation.

## Tests

The default suite is offline and uses fake model services:

```bash
python -m pytest packages/arc-companion/tests
```
