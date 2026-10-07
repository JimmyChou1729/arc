# Explicit environment setup

These are operator-invoked setup and verification steps. `doctor-arc.py` and
report rendering never install dependencies. Select an existing writable
project and a new profile directory; keep them under ignored `local/` when
working inside the ARC checkout.

## Base runtime and network

The default runtime installs HTTPX 0.28.1 and socksio 1.0.0 through the owning
package dependencies and product constraints. Environment proxies remain
enabled for paper providers. `proxy_dependency_missing` identifies an
incomplete installation; use a fresh runtime with the current source lock.
Successful SOCKS client initialization does not prove that external services
are reachable. Diagnose actual DNS, TLS, proxy and HTTP failures separately.

## Restricted-host setup and interruption recovery

Set `AC_RUNTIME_HOME` to a writable private directory, then use the public
`arc-runtime setup` entry point. Bootstrap layout v2 chooses private uv/pip
caches and temporary storage by default. Explicit `UV_CACHE_DIR`,
`PIP_CACHE_DIR`, and the first of `TMPDIR`/`TEMP`/`TMP` remain authoritative;
unwritable explicit paths produce a path-specific error and require correction.
No HOME change, chmod of system directories or sudo is needed for the Python
runtime. `arc-runtime doctor` reports selected paths, readiness, last failure
and a read-only probe of an existing kernel lock. It never installs packages. The
workflow diagnostic `doctor-arc.py` should be invoked through
`arc-runtime script <skill-dir>/scripts/doctor-arc.py`. If it is run in an
interpreter missing AC dependencies, it returns structured
`runtime_dependencies_missing` guidance with the exact public setup/diagnostic
commands and exits 1. It does not install packages or infer that report/provider
checks passed. `--help` remains available before runtime initialization.

After a platform interruption, preserve that platform's cancellation message
separately, then run:

```bash
<skill-dir>/scripts/arc-runtime doctor
<skill-dir>/scripts/arc-runtime setup --retry
```

`lock_occupied` means a coordinator or surviving installer still holds the
kernel lock. `lock_wait_timeout` returns exit 75 without takeover. Inspect or
wait for that existing process; do not delete its lock or endlessly retry.
`lock_recovered` means the kernel lock became available after an unfinished
owner record, not that a user cancelled the work. `lock_ownership_unverifiable`
requires a supported runtime filesystem. No host/PID/TTL heuristic decides
ownership, and platform approval cancellation is not bypassed through elevated
permissions.

Every attempt writes to its own retained directory and log. Only a complete
venv with a matching success marker is published; retries preserve prior
failure evidence. Already-ready v2 environments are reused. New v2 runtime
paths are separate from older v1 paths so legacy clients and uncertain legacy
locks cannot race with recovery. Old state is preserved automatically; manual
lock cleanup is unnecessary. v2 setup currently requires POSIX flock on a
filesystem with reliable locking. Cross-PID-namespace tests simulate foreign
owner records on shared local inodes; DOT's actual namespace/mount behavior and
Windows setup are not certified by those tests.

If runtime setup is not ready, Host, paper access and scientific execution have
not thereby been validated. After it succeeds, run those checks and the
explicit report initialization below separately. PDF failure never requires
rerunning an already accepted scientific calculation.

## Supported PDF profile

The supported Linux reference profile is Ubuntu 24.04 with its packaged
Pandoc, TeX Live XeLaTeX/CJK packages, Fontconfig and Poppler. Initialize those
system prerequisites explicitly in a disposable machine/container or through
the host administrator:

```bash
sudo apt-get update
sudo apt-get install -y pandoc texlive-xetex texlive-lang-chinese \
  texlive-latex-extra texlive-fonts-recommended lmodern fontconfig poppler-utils
```

For Linux x86_64/glibc hosts with Pandoc, Perl, Fontconfig and Poppler but
missing TeX packages, initialize a separate caller-owned TeX tree explicitly:

```bash
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/setup-report.py \
  --tex-dir <new-owned-tex-directory> --output-dir <new-report-profile-directory>
export ARC_REPORT_ENVIRONMENT=<report-profile-directory>/report-environment.json
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/verify-report.py \
  --project-dir <verification-project>
```

This downloads the SHA-256-verified TinyTeX-1 v2026.02 archive (TeX Live 2025,
about 71 MB compressed), then installs xeCJK and its dependency closure from
the frozen TeX Live 2025 final repository. The source lock is
`scripts/_arc_workflows/report-tex.json`. The older fixed distribution is
intentional: both its engine and package repository belong to the same TeX
Live release. No moving `latest` download or cross-year package update is used.
Package revisions, engine version and source identity are saved in
`arc-tex-install.json`; upstream licenses remain in the TeX tree.

Setup invokes only that tree's `tlmgr`, without sudo, system registration or
shell-profile edits. Its temporary paths stay inside the owned attempt. The
destination is published only after staged validation, then checked at its
final path. An intact installation can be reused; corrupt or unrelated trees
are preserved and rejected with advice to choose a new empty directory. Use a
new font profile when switching its recorded TeX tools. The explicit `--tex-dir`
option is required for downloads; font-only setup, doctor and rendering never
install TeX.

### Query and recover an interrupted TeX setup

Use the original destination even if the host execution session is no longer
queryable. First confirm that the Python runtime is already initialized with
`arc-runtime doctor`: the wrapper's `script` command may initialize a missing
Python runtime. With that prerequisite met, the report status script reads
durable records without Pandoc, downloads or tool execution, and does not create
installation directories:

```bash
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/setup-report.py \
  --status --tex-dir <owned-tex-directory> --output-dir <report-profile-directory>
```

The sibling `.<tex-directory-name>.ac-install/` contains the source-bound
operation, retained attempts, phase events, verified archive and command
stdout/stderr/terminal receipts. `running` means its kernel lease is occupied;
a retry returns `installation_busy` and exit 75 without starting another
installer. `interrupted` means the lease is free but no operation completion
was recorded. `failed` retains the command outcome. `unverifiable` requires
inspection rather than takeover. PIDs and timestamps are diagnostic only;
there is no timeout-based lock deletion. Query exit zero means the query
succeeded, not that installation or PDF delivery succeeded.

Once the lease is free, explicitly retry:

```bash
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/setup-report.py \
  --retry --tex-dir <owned-tex-directory> --output-dir <report-profile-directory>
```

Retry preserves every prior mutable TeX tree and creates a new attempt from
the exact SHA-256-verified cached archive. It never resumes an uncertain
`tlmgr` mutation in place. A valid already-published tree is rechecked and
reused, including recovery after publication but before operation completion.
A damaged published tree requires a new empty `--tex-dir`. Source-lock changes
also require a new destination. The permanent lease inode must not be deleted.

Supervised commands stream durable logs and inherit the lease. They remain
under the platform's existing execution authority. If only the coordinator
dies, a surviving installer blocks retry; if the platform terminates the whole
process group/container, the last phase and available receipts remain the
evidence. A platform approval-review cancellation is not proof of user
cancellation or directory permission denial. Do not escalate or loop retries
to bypass that policy. Shared-mount locking remains deployment-specific.

Older installers without these records cannot have their lost execution
session reconstructed. Preserve their files, check any still-running original
process through the host, and use a new empty destination once safe. An
existing archive can be supplied with `--tex-archive <archive.tar.gz>`; its
exact locked size and SHA-256 must match before it is used. This imports only
verified source bytes, never an unknown extracted tree.

On other platforms, provide the documented system tools or an independently
managed TeX installation on `PATH`. The owned installer reports unsupported
platforms explicitly. It requires Python 3.11.8+ or 3.12+ for safe archive
extraction. Network restrictions may block either release/archive downloads
or the frozen package repository; setup reports that failure and retains
accepted scientific work. The ZIP contains source locks and scripts, not TeX
binaries or a venv.

### Provision the report font profile

Provision ARC's report fonts explicitly:

```bash
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/setup-report.py \
  --output-dir <new-report-profile-directory>
export ARC_REPORT_ENVIRONMENT=<report-profile-directory>/report-environment.json
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/doctor-arc.py
<skill-dir>/scripts/arc-runtime script <skill-dir>/scripts/verify-report.py \
  --project-dir <verification-project>
```

Setup downloads Noto Sans CJK SC regular/bold and the SIL Open Font License
from immutable upstream commit `523d033d6cb47f4a80c58a35753646f5c3608a78`
(Sans 2.004). URLs, sizes and SHA-256 digests are in
`scripts/_arc_workflows/report-fonts.json`. The OFL is retained alongside the
fonts. Setup publishes a complete verified profile atomically and reuses an
intact profile without downloading. A damaged or unrelated non-empty directory
is preserved and rejected; choose a new directory to repair it.

The profile records the selected tool locations. Font paths are passed directly
to XeLaTeX's fontspec/xeCJK options; system font registration is unnecessary.
Explicit `--main-font`, `--cjk-font`, `ARC_REPORT_MAIN_FONT`, or
`ARC_REPORT_CJK_FONT` takes priority for the corresponding font. Preflight
checks exact family matching, representative Chinese character coverage, and
the TeX packages required by the current template, including fontspec,
xeCJK and the lmodern package used by distribution Pandoc templates. Reported
missing glyphs prevent publication. Fontconfig checks may be unknown on other platforms, so genuine rendering remains the final check.

Verification uses the original renderer for a real PDF with Chinese, inline
math and display math. It checks pages and extracted text through Poppler;
render its pages with `pdftoppm` and visually inspect the equations. Its JSON
keeps `executed`, `report_delivered`, and `scientific_accepted` separate; this
fixed rendering sample is not a scientific result.

## Delivery retry

Keep the original Markdown and accepted research artifacts. After fixing an
environment problem, rerun only `render-report.py` with the same input and
visible PDF output. Check `delivery_status` and `warnings`; unavailable delivery
intentionally retains exit code 0 under `arc.report_delivery.v2`. A failed
preflight, conversion, or missing-glyph check preserves the previous PDF.

## Optional scientific Python

The base runtime is sufficient for paper/text tasks. For numerical work, select
the explicit requirements file on each setup, doctor or script command:

```bash
<skill-dir>/scripts/arc-runtime --requirements <skill-dir>/scripts/scientific-requirements.txt setup
<skill-dir>/scripts/arc-runtime --requirements <skill-dir>/scripts/scientific-requirements.txt doctor
<skill-dir>/scripts/arc-runtime --requirements <skill-dir>/scripts/scientific-requirements.txt \
  script <skill-dir>/scripts/scientific-python.py --verify
```

This creates a separate runtime containing NumPy 2.3.5 and SciPy 1.17.0, whose
declared Python minimum is 3.11. It does not install into the base runtime.
Keep the same `--requirements` selection when running a worker's numerical
script, or use the private interpreter reported by `scientific-python.py`.
Record that output and the actual script/output hashes in the worker's evidence.
Doctor reports only its current interpreter and metadata; it does not claim
that other host Python environments lack these packages or that an import has
been executed successfully.

The verification fixture integrates the dimensionless scalar IVP for
`mu=m/H` equal to 0, 1, 1.5 and 2. Two integration methods are compared against
the corresponding analytic solutions on a finite sample grid, using mixed
absolute/relative tolerance near zeros. This is numerical compatibility
evidence, not a strict global error bound or a substitute for independent
calculator/referee work. `scientific_accepted` remains null for this fixture.

SymPy and Matplotlib are optional. Select a caller-owned requirements file with
tested exact pins when a task needs them. Optional files accept plain exact
package pins and cannot override the packages owned by the source lock.
Changing their contents selects another runtime fingerprint. No dependency
installation occurs in doctor, model calls or ordinary report rendering.

References:

- [HTTPX SOCKS support](https://www.python-httpx.org/advanced/proxies/)
- [Pandoc PDF prerequisites](https://pandoc.org/MANUAL.html#creating-a-pdf)
- [Noto CJK source and license](https://github.com/notofonts/noto-cjk/tree/523d033d6cb47f4a80c58a35753646f5c3608a78)
- [User-owned TinyTeX](https://yihui.org/tinytex/)
