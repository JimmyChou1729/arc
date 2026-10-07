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

## Supported PDF profile

The supported Linux reference profile is Ubuntu 24.04 with its packaged
Pandoc, TeX Live XeLaTeX/CJK packages, Fontconfig and Poppler. Initialize those
system prerequisites explicitly in a disposable machine/container or through
the host administrator:

```bash
sudo apt-get update
sudo apt-get install -y pandoc texlive-xetex texlive-lang-chinese \
  texlive-latex-extra texlive-fonts-recommended fontconfig poppler-utils
```

On another host, provide equivalent tools on `PATH`. ARC does not invoke
sudo, alter system fonts, or update an existing TeX installation. Where system
installation is unavailable, the profile remains unavailable until the host
provides those prerequisites. A separate user-owned TinyTeX installation can
also supply them; configure its bin directory on `PATH` and use its documented
package management rather than modifying an unrelated system installation.

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
the TeX packages required by the current template, including fontspec and
xeCJK. Reported missing glyphs prevent publication. Fontconfig checks may be
unknown on other platforms, so genuine rendering remains the final check.

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

References:

- [HTTPX SOCKS support](https://www.python-httpx.org/advanced/proxies/)
- [Pandoc PDF prerequisites](https://pandoc.org/MANUAL.html#creating-a-pdf)
- [Noto CJK source and license](https://github.com/notofonts/noto-cjk/tree/523d033d6cb47f4a80c58a35753646f5c3608a78)
- [User-owned TinyTeX](https://yihui.org/tinytex/)
