"""Project-local Markdown-to-PDF delivery."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


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
        raise ValueError(f"{label} must be inside the project directory") from exc
    if visible and any(part.startswith(".") for part in relative.parts):
        raise ValueError(f"{label} must use a visible project path")
    return resolved


def render_markdown_pdf(
    *,
    project_dir: str | Path,
    source: str | Path,
    output: str | Path,
) -> Path:
    project = Path(project_dir).expanduser().resolve()
    if not project.is_dir():
        raise ValueError("project directory does not exist")
    source_path = project_path(source, project, label="input")
    output_path = project_path(output, project, label="output", visible=True)
    if source_path.suffix.lower() != ".md" or not source_path.is_file():
        raise ValueError("input must be a readable Markdown file")
    if output_path.suffix.lower() != ".pdf":
        raise ValueError("output must use the .pdf suffix")

    scratch = project / ".arc" / "report-render"
    scratch.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="render-", dir=scratch) as temporary:
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
            raise RuntimeError(
                f"Pandoc/XeLaTeX report rendering failed ({completed.returncode}): "
                f"{detail or 'no diagnostic output'}"
            )
        try:
            if not rendered.is_file() or not rendered.read_bytes().startswith(b"%PDF-"):
                raise RuntimeError("Pandoc did not produce a valid PDF file")
        except OSError as exc:
            raise RuntimeError("rendered PDF is unreadable") from exc

        descriptor, staged_name = tempfile.mkstemp(
            prefix=".arc-report-", suffix=".pdf", dir=output_path.parent
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
        raise ValueError("source delivery is not a readable file")
    if source_path.suffix.lower() != ".pdf" or output_path.suffix.lower() != ".pdf":
        raise ValueError("visible copied deliveries must use the .pdf suffix")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(
        prefix=".arc-delivery-", suffix=output_path.suffix, dir=output_path.parent
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
    return output_path


__all__ = ["project_path", "publish_visible_copy", "render_markdown_pdf"]
