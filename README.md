# ARC

Agent Research Copilot (ARC) is the research layer for theoretical-physics
papers. It provides academic-paper access and source-aware research-domain,
idea, planning, calculation, and checking workflows.

ARC v2 depends on [AC Foundation](https://github.com/tririver/ac-foundation)
for durable jobs, model calls, neutral documents, and proposer-reviewer
orchestration. OCR proofreading, translation, Companion readers, and HTML
publication belong to [ALC](https://github.com/tririver/alc).

## Packages

- `arc-paper`: arXiv, INSPIRE, DOI, paper caching, structural reads, citation
  traversal, and summaries.
- `arc-domain`: evidence-bearing research-domain construction and exports.

The public `arc-paper` API is unchanged by the v2 repository split. Foundation
commands remain available through `arc-runtime`; the plugin exposes only the
`arc-runtime`, `arc-paper`, and `arc-domain` wrappers.

## Install

ARC is distributed as an agent plugin with a lazy, SHA-locked private runtime.
The runtime installs exact Git revisions of AC Foundation and ARC; no PyPI
publication is assumed.

For Codex:

```bash
codex plugin marketplace add tririver/arc --ref stable
codex plugin add arc@arc
```

For Claude Code:

```text
/plugin marketplace add tririver/arc@stable
/plugin install arc
```

For DeepSeek Harness:

```bash
dsh plugin --profile arc add github:tririver/arc
```

Check or prewarm the locked runtime:

```bash
plugins/arc/bin/arc-runtime doctor
plugins/arc/bin/arc-runtime setup
```

The default paper cache is `.arc/cache/arc-paper` below the launch directory;
override it with `ARC_PAPER_CACHE`. Foundation runtime and neutral document
state use `AC_HOME`, `AC_RUNTIME_HOME`, and `AC_DOCUMENT_CACHE`.

## Use

Ask an installed agent directly for ordinary paper outcomes:

```text
Summarize arXiv:0911.3380.
Find its references and citing papers.
Show the context around equation 2.30.
```

Name ARC for managed research workflows:

```text
Use ARC to build a domain from arXiv:0911.3380 with papers since 2024.
Use ARC to develop and review ideas from that domain.
Use ARC to check this calculation.
```

## Citation

If ARC contributes to your research, please cite:

Yanjiao Ma, Yi Wang, and Xingkai Zhang. *ARC: An LLM-Native Agent Workflow
for Theoretical Physics Research*. ChinaXiv:202606.00234, 2026.
https://chinaxiv.org/abs/202606.00234

## Development

Python 3.11 or newer is required. Read `AGENTS.md`, keep generated work below
ignored `local/`, and run:

```bash
AC_FOUNDATION_REPO_ROOT=../ac-foundation python scripts/check-generated-foundation.py
PYTHONPATH="$(find ../ac-foundation/packages packages -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)" \
  python -m pytest --import-mode=importlib packages/*/tests tests
scripts/build-packages.sh
```

Prepare an approved release from a clean checkout with
`scripts/release-arc.sh <version>`. The script updates both packages and plugin
manifests, validates them, and pins the release source commit; it does not tag
or publish.
