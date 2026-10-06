"""Project-local Markdown-to-PDF delivery."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class ReportDeliveryContractError(ValueError):
    """A caller supplied an invalid project-local delivery request."""


class ReportDeliveryUnavailable(RuntimeError):
    """A valid delivery request could not be rendered or published."""


def report_fonts(main_font: str | None = None, cjk_font: str | None = None) -> dict[str, str]:
    fonts = {"mainfont": main_font if main_font is not None else os.environ.get("ARC_REPORT_MAIN_FONT", "Noto Sans CJK SC"),
             "CJKmainfont": cjk_font if cjk_font is not None else os.environ.get("ARC_REPORT_CJK_FONT", "Noto Sans CJK SC")}
    for name, value in fonts.items():
        if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(char in value for char in "\x00\r\n"):
            raise ReportDeliveryContractError(f"{name} must be a non-empty font family name of at most 200 characters")
    return fonts


def report_dependencies(*, main_font: str | None = None, cjk_font: str | None = None) -> dict:
    """Probe tools and exact font families without rendering a report."""
    tools = {name: shutil.which(name) is not None for name in ("pandoc", "xelatex", "fc-list")}
    try:
        fonts = report_fonts(main_font, cjk_font)
    except ReportDeliveryContractError as exc:
        return {"tools": tools, "fonts": {}, "status": "invalid_configuration", "guidance": str(exc)}
    families = None
    if tools["fc-list"]:
        try:
            completed = subprocess.run(["fc-list", "--format", "%{family}\n"], capture_output=True, text=True, timeout=10, check=False)
            if completed.returncode == 0:
                families = {family.strip().casefold() for line in completed.stdout.splitlines() for family in line.split(",")}
        except (OSError, subprocess.TimeoutExpired):
            pass
    checks = {name: {"family": family, "status": "unknown" if families is None else "available" if family.casefold() in families else "missing"}
              for name, family in fonts.items()}
    return {"tools": tools, "fonts": checks,
            "status": "unavailable" if not tools["pandoc"] or not tools["xelatex"] or any(item["status"] == "missing" for item in checks.values()) else "font_check_unavailable" if families is None else "available",
            "guidance": "Install Pandoc/XeLaTeX and the chosen fonts, or set ARC_REPORT_MAIN_FONT and ARC_REPORT_CJK_FONT. Retry delivery with the existing Markdown; scientific runs need not restart."}


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
) -> Path:
    fonts = report_fonts(main_font, cjk_font)
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
            completed = subprocess.run(
                command,
                cwd=project,
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip()
                raise ReportDeliveryUnavailable(
                    "Pandoc/XeLaTeX report rendering failed "
                    f"({completed.returncode}): "
                    f"{detail or 'no diagnostic output'}"
                )
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
