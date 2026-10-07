# Portable plugin bundles

ARC keeps a portable root `plugins/arc/plugin.json`, automatically discovered
`skills/`, and `assets/`. The Codex and Claude compatibility manifests retain
the same identity and release version. OpenAI presentation in the portable
extension replaces the whole Codex overlay; the two presentations are kept
identical rather than relying on field merging.

## Build and inspect

Use Python 3.11+ with the development tools `jsonschema`, `PyYAML`, and
`Pillow` installed explicitly in an isolated environment. No model, network,
installation, publication, or runtime-pin update occurs during packaging:

```bash
python scripts/plugin_bundle.py build --profile private-local \
  --output local/plugin-bundles/arc-private-local-dev.zip
python scripts/plugin_bundle.py build --profile public-skills \
  --output local/plugin-bundles/arc-public-skills-dev.zip
python scripts/plugin_bundle.py validate --profile public-skills \
  local/plugin-bundles/arc-public-skills-dev.zip
```

Each successful build emits the ZIP, a `.sha256` sidecar, and a
`.manifest.json` with per-file hashes, sizes, profile, and validation results.
Entries are sorted with fixed timestamps, compression settings, and script
modes. Known development/cache directories are excluded from construction;
validation rejects them if already present in a ZIP. Symlinks are rejected.

`private-local` preserves ARC's native wrappers and DSH adapter. The
`public-skills` archive includes the portable manifest, both compatibility
manifests, skills, icons, and the original MIT license; it omits `bin/` and
`dsh/`, since the skill uses its own runtime scripts. Public construction does
not silently remove app, hook, MCP, or screenshot declarations. Such source
configurations fail validation and need an explicit separate distribution
design. Private component declarations are limited to contained JSON-file
references; the tool does not validate or attest private registered app IDs.

## Validation boundaries

The report separates the vendored official portable JSON Schema from package
checks, final skills-only listing-format checks, and ARC's cleanliness rules.
All compatibility manifests are inspected even when shadowed. Both icons are
required by ARC's packaging policy. SVG icons use numeric square viewBoxes;
bitmap icons must be supported square images of 48–4096 pixels and at most
5 MiB. YAML skill frontmatter and optional `agents/openai.yaml` are inspected.

ZIP limits follow the official error reference: 100 MB compressed, 100 MiB
per entry, 512 MiB expanded, 5,000 entries, and 20 path segments. Paths must
be relative POSIX paths without duplicate, case/Unicode normalization, or
file/directory conflicts. ARC additionally limits paths to 1,024 UTF-8 bytes
and excludes downloaded PDFs, binary libraries, credentials, experiments,
machine-specific home paths, and task artifacts. No validator can attest that
arbitrary source text contains no secrets; review the final inventory before
sharing it.

## Test a clean extraction

`tests/test_plugin_bundle.py` extracts each profile in a new directory and
runs workflow doctor and an offline public `ac-llm` export/submit/resume cycle,
including matching duplicate submission and completed replay. It uses the
development-installed Foundation packages and the full brokered host-turn
contract. This proves packaged script portability with that environment,
not the currently published runtime lock.

The separate `fresh-plugin` CI job runs `scripts/verify-fresh-plugin.py` on the
final ZIP outside the checkout with a new runtime/cache and no source overlays.
It verifies the six installed package Git SHAs, constraints digest, default
SOCKS dependency, the public Host acceptance/replay cycle, the optional numerical
profile, and real Chinese/math PDF delivery on the documented Ubuntu profile.
The Linux test job also executes the locked Foundation bootstrap/recovery
suite, including a real uv subprocess that retains its kernel lock after the
coordinator is killed. Foreign PID namespace ownership records are simulated;
this does not certify a deployment's namespace or shared-mount configuration.

Cold CI installs with real uv while `XDG_CACHE_HOME` points at a read-only
system path. `--verify-private-cache` requires a populated private uv cache,
and repeated public `setup` / `setup --retry` must retain the success marker.
The report check also inspects the public renderer's `delivery_status`; exit
code zero alone is insufficient. Attempt logs and state are retained in CI
artifacts, including unsuccessful installs.

The CI matrix validates both preinstalled system TeX and the explicit owned
TeX setup. Its PDF pages, package installation receipt and machine-readable
evidence are retained as separately named CI artifacts. Add `--owned-tex` to
`--report` when testing the latter locally on Linux x86_64/glibc.
The owned profile also queries read-only installation status before/after
setup and repeats explicit `--retry` on a ready tree without creating another
attempt. Phase events, command logs and terminal receipts are uploaded without
the downloaded archive or TeX binaries. Offline subprocess tests separately
kill the TeX coordinator, prove the surviving command blocks concurrent retry,
then recover from the verified archive in a new mutable attempt. This simulates
process interruption; it does not reproduce a host approval service or certify
an arbitrary shared mount.

Default CI remains offline for research-provider requests. The manual
`workflow_dispatch` input `run_network=true` explicitly enables the two real
paper integration tests in a separate step; a skipped network step is not a
passed network check. The cold verifier records `not_requested` for optional
checks that were not selected. Keep host-local test records such as
`verification.json` and absolute runtime paths outside distributed ZIPs; ship
provenance and test evidence as separate artifacts.

For a local cold check, use a new directory outside the source checkout:

```bash
python scripts/verify-fresh-plugin.py --zip local/plugin-bundles/arc-public-skills-dev.zip \
  --output-dir <new-directory-outside-checkout> --report --scientific
```

Real paper access remains opt-in: set `ARC_RUN_NET_TESTS=1` and add
`--network-paper arXiv:0911.3380`. It starts with an empty paper cache, reads
metadata/HTML/one section, then uses the exact cached document handle with
network providers blocked to verify an unchanged warm-cache fingerprint.
Installation downloads are separate from this opt-in research-network check.
External-service failures retain their raw evidence; they are not counted as
successful end-to-end paper access.

The normal Linux CI job requires DSH socket tests with
`ARC_REQUIRE_DSH_TESTS=1`; missing Node.js fails this required check. Other
test hosts may skip when Node.js is absent. With Node.js installed, they may
skip only a probe-confirmed EPERM/EACCES restriction, with the explicit reason;
other failures remain failures. Host model handoff does not depend on the DSH socket bridge.

To test a fresh source-override installation, set both checked-out roots:

```bash
AC_INSTALL_SOURCE=local \
AC_FOUNDATION_REPO_ROOT=<foundation-checkout> \
AC_PRODUCT_REPO_ROOT=<arc-checkout> \
AC_RUNTIME_HOME=<new-development-runtime-directory> \
  <skill-dir>/scripts/arc-runtime setup
```

Use a new runtime directory; never patch an existing content-addressed runtime.
Source overrides and default Git installation are separate checks. The
upstream `dev` lock selects immutable, tested commits from
`tririver/ac-foundation` and `tririver/arc` so a development ZIP can install the
Host code without local checkouts or contributor forks. Use the ARC `dev`
branch when preparing a team test build, and retain its source commit in the
separate test evidence. CI reads both the repository URL and SHA from that lock.
Generated Foundation copies and their checksums must match its Foundation SHA.
The release version remains unchanged until approval. Public release locks
must select the final approved, reachable release commits.

## Public submission

Format compliance, an installable release, and public submission readiness
are separate states. The offline report always leaves
`public_submission_ready=false` and `runtime_content_verified=false`;
it does not perform publisher verification, skill scans, policy attestations,
or actual Work/Cowork/dot installation. The current skills-only ZIP rules make
listing URLs optional; supplied URLs still need content/access verification.
Keep the original upstream author identity rather than inventing a verified
publisher. Skills-only submissions do not require MCP test cases, demo videos,
or reviewer credentials. Future MCP functionality requires the With MCP route.

Before submission, approve a new distribution version and runtime pins,
complete target-host smoke tests, choose a verified publisher, provide release
and country-targeting details where required, and complete portal scans and
attestations. Upload, review submission, and public publication remain separate
authorized actions.

Official references checked on 2026-10-06:

- [Agent Plugins schema](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json)
- [Package structure](https://developers.openai.com/plugins/build/plugins)
- [Submission restrictions and limits](https://developers.openai.com/plugins/deploy/submission-errors)
- [Public submission](https://developers.openai.com/plugins/deploy/submission)
