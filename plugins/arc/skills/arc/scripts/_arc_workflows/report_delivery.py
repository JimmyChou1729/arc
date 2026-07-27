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
) -> Path:
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
                "-V",
                "mainfont=Noto Sans CJK SC",
                "-V",
                "CJKmainfont=Noto Sans CJK SC",
            ]
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
