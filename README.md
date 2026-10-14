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
codex plugin marketplace add tririver/arc --ref main
codex plugin add arc@arc
```

For Claude Code:

```text
/plugin marketplace add tririver/arc@main
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

After setup, diagnose providers, the Host coordinator, proxy dependencies,
optional scientific libraries, and PDF tools/fonts without model calls:

```bash
plugins/arc/bin/arc-runtime script plugins/arc/skills/arc/scripts/doctor-arc.py \
  --project-dir <existing-project>
plugins/arc/bin/arc-runtime ac-llm doctor --provider host --environment
```

Runtime `doctor` checks lock identity/readiness; workflow doctor checks the
active Python environment. Neither probes network access or authentication.
Host coordination is described in
[`host-execution.md`](plugins/arc/skills/arc/manuals/host-execution.md).
Explicit report-font provisioning, TeX prerequisites, real PDF verification,
and an optional numerical Python environment are documented in
[`environment-setup.md`](plugins/arc/skills/arc/manuals/environment-setup.md).
During development, use both `AC_FOUNDATION_REPO_ROOT` and
`AC_PRODUCT_REPO_ROOT` with `AC_INSTALL_SOURCE=local`; changing only the code
does not update the published SHA-locked runtime.

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
export AC_FOUNDATION_REPO_ROOT="$(cd ../ac-foundation && pwd)"
python scripts/check-generated-foundation.py
PYTHONPATH="$(find "$AC_FOUNDATION_REPO_ROOT/packages" "$PWD/packages" -mindepth 2 -maxdepth 2 -type d -name src -print | paste -sd: -)" \
  python -m pytest --import-mode=importlib packages/*/tests tests
scripts/build-packages.sh
```

Use a development interpreter with the test dependencies installed. Generic
tests isolate inherited `AC_LLM_HOST_COORDINATOR` and `ARC_REPORT_ENVIRONMENT`;
Host/report tests set their own profiles explicitly. Source-root settings remain
active so imported package provenance is still checked.

The upstream `dev` branch is the shared preview for team testing. Development
pushes retain the current version; identify a test build by its commit and
runtime source lock. The optional [packaging tools](docs/plugin-packaging.md)
remain available to hosts that need to build their own plugin archive.

## Version tags

Formal versions use immutable `vMAJOR.MINOR.PATCH` tags. CI validates tags and
builds Python packages; it does not create GitHub Releases, publish artifacts,
or run the former ZIP cold-install job. A successful CI run does not certify
that a host-built ZIP can be uploaded or installed in a particular host.

1. Prepare an approved version on a clean task branch with
   `scripts/release-arc.sh <version>`. It updates package and plugin versions,
   tests and builds them, then creates a source commit and a separate product
   pin commit. It does not create or push tags.
2. Merge the Foundation changes first. Set the runtime lock to the official
   `tririver/ac-foundation` commit on `main`, and synchronize generated copies
   and their checksums. Submit the ARC changes through a PR with official
   upstream source URLs. Preserve the pinned product commit when merging;
   if squash/rebase changes its identity, update the pin to the final upstream
   source commit before tagging. Its `packages` tree must match the tagged tree.
3. After the final upstream `main` commit passes CI, and publication is
   approved, create an annotated tag on that exact commit and push only that
   tag to upstream. The tag must match `VERSION`, both Python packages and all
   plugin manifests. Do not move an existing version tag; use a new version
   for corrections.
4. Check the tag CI result before recommending the version. It verifies main
   ancestry, package versions, source pins, generated files and the existing
   offline tests/builds. A failed check leaves the tag present but unverified;
   tag creation itself is not validation. Consumers should use the verified
   tag when asking their host to package the plugin.

For a local version check, run `python scripts/check-release-tag.py --tag vX.Y.Z`.
For source provenance also pass `--check-pins --foundation-root <checkout>`;
the Foundation checkout must be at the locked SHA with `origin/main` available.
The CI ancestry check uses the repository's `origin/main`; a fork tag is not
an upstream release. Installation follows `main`; `stable` preserves the
previous upstream baseline and is managed separately from version tags.
