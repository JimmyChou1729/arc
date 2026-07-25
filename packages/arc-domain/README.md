# arc-domain

`arc-domain` builds a reusable research-domain package from a seed paper and
scientific intent. It owns foundation and domain-paper selection, citation
graphs, evidence and paper packs, domain summaries, HTML rendering, and
published domain generations. It calls `arc-paper` for paper data and
`arc-llm` for model work.

Durable builds accept only the closed `arc.domain_build_policy.v2` and
`arc.domain_build_request.v2` contracts. The default policy infers the
foundation from the seed and combines representative and recent citers;
callers may instead select fixed-seed and strict-window modes explicitly.
Exported summary packages accept the current `arc.domain_summary.v5`
contract.

## Quick start

Build one domain with an explicit research intent:

```bash
arc-domain build "<seed-paper-id>" --intent "<research-intent>"
```

Replace both quoted placeholders with the seed identifier and the scientific
focus for the domain.
The result reports durable run and domain identities. Use `arc-domain --help`
and `arc-domain build --help` for build policy, inspection, published-artifact
queries, and run controls.

## Tests

The package suite uses fake paper and model services by default:

```bash
python -m pytest packages/arc-domain/tests
```
