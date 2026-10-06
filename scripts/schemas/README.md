# Manifest schema provenance

`agent-plugin-1.0.0.json` is the unmodified Agent Plugins 1.0.0 schema
downloaded from
<https://agent-plugins.org/schemas/1.0.0/plugin.schema.json> on 2026-10-06.
SHA-256: `0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883`.

The portable schema does not validate client extension semantics. ARC's
packager separately checks compatibility manifests, OpenAI listing fields,
component restrictions, skill metadata, paths, and assets. These rules were
checked against the official package and submission-error documentation on
2026-10-06. Recheck them when preparing a public release; the offline validator
does not claim portal or publisher approval.
