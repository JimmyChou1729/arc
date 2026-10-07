"""Project-local Markdown-to-PDF delivery."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from _arc_workflows.report_environment import (
    ReportEnvironmentError, load_report_environment, report_process_environment,
)

REQUIRED_TEX_PACKAGES = ("fontspec.sty", "xeCJK.sty", "unicode-math.sty", "amsmath.sty", "geometry.sty", "bookmark.sty", "xcolor.sty", "longtable.sty", "booktabs.sty", "fancyvrb.sty", "graphicx.sty")


class ReportDeliveryContractError(ValueError):
    """A caller supplied an invalid project-local delivery request."""


class ReportDeliveryUnavailable(RuntimeError):
    """A valid delivery request could not be rendered or published."""

    def __init__(self, message, *, code="pdf_render_unavailable"):
        super().__init__(message)
        self.code = code


def report_fonts(main_font: str | None = None, cjk_font: str | None = None) -> dict[str, str]:
    fonts = {"mainfont": main_font if main_font is not None else os.environ.get("ARC_REPORT_MAIN_FONT", "Noto Sans CJK SC"),
             "CJKmainfont": cjk_font if cjk_font is not None else os.environ.get("ARC_REPORT_CJK_FONT", "Noto Sans CJK SC")}
    for name, value in fonts.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(char in value for char in "\x00\r\n"):
            raise ReportDeliveryContractError(f"{name} must be a non-empty font family name of at most 200 characters")
    return fonts


def _settings(main_font=None, cjk_font=None, environment=None):
    fonts = report_fonts(main_font, cjk_font)
    try:
        profile = load_report_environment(environment)
    except ReportEnvironmentError as exc:
        raise ReportDeliveryContractError(str(exc)) from exc
    options = {}
    if profile:
        directory = profile["font_directory"]
        if any(char in directory for char in "{}\x00\r\n"):
            raise ReportDeliveryContractError("Report font directory contains unsupported TeX path characters")
        for name, explicit in (("mainfont", main_font is not None or "ARC_REPORT_MAIN_FONT" in os.environ),
                               ("CJKmainfont", cjk_font is not None or "ARC_REPORT_CJK_FONT" in os.environ)):
            if not explicit:
                fonts[name] = "NotoSansCJKsc-Regular.otf"
                options["mainfontoptions" if name == "mainfont" else "CJKoptions"] = f"Path={{{directory}/}},BoldFont=NotoSansCJKsc-Bold.otf"
    return fonts, options, profile, report_process_environment(profile)


def _probe(command, env):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False, env=env)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _covers_chinese(charset):
    ranges = []
    try:
        for item in charset.split():
            bounds = item.split("-")
            ranges.append((int(bounds[0], 16), int(bounds[-1], 16)))
    except ValueError:
        return False
    return all(any(start <= ord(char) <= end for start, end in ranges) for char in "中文数学验证")


def report_dependencies(*, main_font=None, cjk_font=None, environment=None) -> dict:
    """Read tool, TeX-package and font coverage state without installing anything."""
    try:
        fonts, options, profile, env = _settings(main_font, cjk_font, environment)
    except ReportDeliveryContractError as exc:
        return {"tools": {}, "fonts": {}, "tex_packages": {}, "status": "invalid_configuration", "guidance": str(exc)}
    tools = {name: shutil.which(name, path=env.get("PATH")) is not None for name in ("pandoc", "xelatex", "kpsewhich", "fc-match", "fc-scan")}
    tex = {name: bool(_probe(["kpsewhich", name], env)) if tools["kpsewhich"] else None for name in REQUIRED_TEX_PACKAGES}
    checks = {}
    for name, family in fonts.items():
        managed = ("mainfontoptions" if name == "mainfont" else "CJKoptions") in options
        command = ["fc-scan", "--format", "%{family}\n%{charset}", str(Path(profile["font_directory"]) / family)] if managed else ["fc-match", "--format", "%{family}\n%{charset}", family]
        raw = _probe(command, env) if tools[command[0]] else None
        status, coverage = "unknown", None
        if raw is not None:
            lines = raw.splitlines()
            matches = bool(lines) and (managed or family.casefold() in {part.strip().casefold() for part in lines[0].split(",")})
            coverage = _covers_chinese(lines[1]) if len(lines) > 1 else False
            status = "available" if matches and (name != "CJKmainfont" or coverage) else "missing" if not matches else "missing_cjk_coverage"
        checks[name] = {"family": family, "status": status, "cjk_sample_coverage": coverage, "managed": managed}
    status = "available"
    if not all(tools[name] for name in ("pandoc", "xelatex", "kpsewhich")):
        status = "tools_missing"
    elif not all(tex.values()):
        status = "tex_packages_missing"
    elif any(item["status"] in {"missing", "missing_cjk_coverage"} for item in checks.values()):
        status = "fonts_missing"
    elif any(item["status"] == "unknown" for item in checks.values()):
        status = "font_check_unavailable"
    return {"tools": tools, "fonts": checks, "tex_packages": tex, "status": status,
            "environment": None if profile is None else profile["path"],
            "guidance": "Use the explicit report setup and documented system prerequisites. Retry delivery from the existing Markdown; accepted scientific results need not restart."}


def project_path(
    value: str | Path,
    project: Path,
    *,
    label: str,
    visible: bool = False,
) -> Path:
    resolved = Path(value).expanduser().resolve()
    try:
        relative = resolved.relative_to(project)
    except ValueError as exc:
        raise ReportDeliveryContractError(
            f"{label} must be inside the project directory"
        ) from exc
    if visible and any(part.startswith(".") for part in relative.parts):
        raise ReportDeliveryContractError(
            f"{label} must use a visible project path"
        )
    return resolved


def render_markdown_pdf(
    *,
    project_dir: str | Path,
    source: str | Path,
    output: str | Path,
    main_font: str | None = None,
    cjk_font: str | None = None,
    environment: str | Path | None = None,
) -> Path:
    fonts, font_options, profile, env = _settings(main_font, cjk_font, environment)
    project = Path(project_dir).expanduser().resolve()
    if not project.is_dir():
        raise ReportDeliveryContractError("project directory does not exist")
    source_path = project_path(source, project, label="input")
    output_path = project_path(output, project, label="output", visible=True)
    if source_path.suffix.lower() != ".md" or not source_path.is_file():
        raise ReportDeliveryContractError(
            "input must be a readable Markdown file"
        )
    if output_path.suffix.lower() != ".pdf":
        raise ReportDeliveryContractError("output must use the .pdf suffix")

    diagnostic = report_dependencies(main_font=main_font, cjk_font=cjk_font, environment=environment)
    codes = {"tools_missing": "pdf_tools_missing", "tex_packages_missing": "pdf_tex_packages_missing", "fonts_missing": "pdf_fonts_missing"}
    if diagnostic["status"] in codes:
        missing = [name for name, available in diagnostic["tex_packages"].items() if available is False]
        raise ReportDeliveryUnavailable(
            f"PDF preflight {diagnostic['status']}: " + (", ".join(missing) or str(diagnostic["fonts"])) + ". " + diagnostic["guidance"],
            code=codes[diagnostic["status"]],
        )

    try:
        scratch = project / ".arc" / "report-render"
        scratch.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="render-", dir=scratch
        ) as temporary:
            rendered = Path(temporary) / "report.pdf"
            command = [
                "pandoc",
                str(source_path),
                "-o",
                str(rendered),
                "--pdf-engine=xelatex",
                f"--resource-path={source_path.parent}{os.pathsep}.",
                "-V",
                "geometry:margin=1.5cm",
            ]
            for name, family in fonts.items():
                command.extend(["-V", f"{name}={family}"])
            for name, value in font_options.items():
                command.extend(["-V", f"{name}={value}"])
            completed = subprocess.run(
                command,
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ReportDeliveryUnavailable(
                    "Pandoc/XeLaTeX report rendering failed "
                    f"({completed.returncode}): "
                    f"{detail or 'no diagnostic output'}"
                )
            if "Missing character:" in completed.stderr:
                raise ReportDeliveryUnavailable("PDF rendering reported missing glyphs; the previous report was preserved.", code="pdf_glyphs_missing")
            if not rendered.is_file() or not rendered.read_bytes().startswith(b"%PDF-"):
                raise ReportDeliveryUnavailable(
                    "Pandoc did not produce a valid PDF file"
                )

            descriptor, staged_name = tempfile.mkstemp(
                prefix=".arc-report-",
                suffix=".pdf",
                dir=output_path.parent,
            )
            os.close(descriptor)
            staged = Path(staged_name)
            try:
                shutil.copyfile(rendered, staged)
                with staged.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(staged, output_path)
            finally:
                staged.unlink(missing_ok=True)
    except ReportDeliveryUnavailable:
        raise
    except subprocess.TimeoutExpired as exc:
        raise ReportDeliveryUnavailable(
            "Pandoc/XeLaTeX report rendering timed out"
        ) from exc
    except OSError as exc:
        raise ReportDeliveryUnavailable(
            f"PDF delivery is unavailable: {exc}"
        ) from exc
    return output_path


def publish_visible_copy(
    *,
    project_dir: str | Path,
    source: str | Path,
    output: str | Path,
) -> Path:
    project = Path(project_dir).expanduser().resolve()
    source_path = project_path(source, project, label="source")
    output_path = project_path(output, project, label="output", visible=True)
    if not source_path.is_file():
        raise ReportDeliveryContractError(
            "source delivery is not a readable file"
        )
    if source_path.suffix.lower() != ".pdf" or output_path.suffix.lower() != ".pdf":
        raise ReportDeliveryContractError(
            "visible copied deliveries must use the .pdf suffix"
        )
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, staged_name = tempfile.mkstemp(
            prefix=".arc-delivery-",
            suffix=output_path.suffix,
            dir=output_path.parent,
        )
        os.close(descriptor)
        staged = Path(staged_name)
        try:
            shutil.copyfile(source_path, staged)
            with staged.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(staged, output_path)
        finally:
            staged.unlink(missing_ok=True)
    except OSError as exc:
        raise ReportDeliveryUnavailable(
            f"PDF publication is unavailable: {exc}"
        ) from exc
    return output_path


__all__ = [
    "ReportDeliveryContractError",
    "ReportDeliveryUnavailable",
    "project_path",
    "publish_visible_copy",
    "render_markdown_pdf",
]
