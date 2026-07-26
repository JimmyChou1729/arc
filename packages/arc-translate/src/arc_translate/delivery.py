"""Publish human-readable Translation results outside project runtime state."""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_bytes

from .project import TranslationProject
from .workflow import BlocksResult, GlossaryResult, LanguageResult


class TranslationDeliveryError(RuntimeError):
    code = "translation_delivery_invalid"


def publish_translation_html(
    project: TranslationProject,
    *,
    run_id: str,
    result: LanguageResult | GlossaryResult | BlocksResult,
) -> Path:
    """Atomically publish the selected successful step as a standalone HTML file."""

    if not run_id:
        raise ValueError("run_id must be non-empty")
    payload = _render(result, run_id=run_id).encode("utf-8")
    atomic_write_bytes(project.delivery_html, payload)
    validate_translation_html(project, run_id=run_id)
    return project.delivery_html


def validate_translation_html(project: TranslationProject, *, run_id: str) -> None:
    try:
        content = project.delivery_html.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TranslationDeliveryError(
            "translation HTML delivery is unreadable"
        ) from exc
    if (
        "<!doctype html>" not in content.casefold()
        or f'data-run-id="{escape(run_id, quote=True)}"' not in content
    ):
        raise TranslationDeliveryError(
            "translation HTML delivery is not bound to the selected result"
        )


def _render(
    result: LanguageResult | GlossaryResult | BlocksResult,
    *,
    run_id: str,
) -> str:
    if isinstance(result, LanguageResult):
        title = "ARC Translation Language Report"
        body = "".join(
            (
                _definition("Source language", result.language_tag),
                _definition("Classification", result.classification),
                _definition("Confidence", f"{result.confidence:.2f}"),
                _definition("Target language", result.target_language),
                _definition("Translation mode", result.mode),
            )
        )
    elif isinstance(result, GlossaryResult):
        title = "ARC Translation Glossary"
        rows = "".join(
            "<tr>"
            f"<td>{escape(str(entry['term']))}</td>"
            f"<td>{escape(str(entry['preferred_translation']))}</td>"
            f"<td>{escape(str(entry['target_definition']))}</td>"
            "</tr>"
            for entry in result.entries
        )
        body = (
            f"<p>Target language: <strong>{escape(result.target_language)}</strong>. "
            f"{len(result.entries)} reviewed terms.</p>"
            "<table><thead><tr><th>Source term</th><th>Translation</th>"
            f"<th>Definition ({escape(result.target_language)})</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )
    else:
        title = "ARC Translation"
        blocks = "".join(
            "<section class=\"translation-block\">"
            f"<h2>{escape(str(item['block_id']))}</h2>"
            f"<p>{_paragraphs(str(item['text']))}</p>"
            "</section>"
            for item in result.translations
        )
        body = (
            f"<p>Source language: <strong>{escape(result.source_language)}</strong>. "
            f"Target language: <strong>{escape(result.target_language)}</strong>. "
            f"Mode: <strong>{escape(result.mode)}</strong>.</p>{blocks}"
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    body {{ max-width: 58rem; margin: 2rem auto; padding: 0 1rem; color: #161616; font: 1rem/1.6 system-ui, sans-serif; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #bbb; padding: .55rem; text-align: left; vertical-align: top; }}
    th {{ background: #f0f3f8; }}
    .translation-block {{ border-top: 1px solid #ddd; margin-top: 1.5rem; }}
  </style>
</head>
<body data-run-id="{escape(run_id, quote=True)}">
  <header><h1>{escape(title)}</h1></header>
  <main>{body}</main>
</body>
</html>
"""


def _definition(label: str, value: str) -> str:
    return f"<p><strong>{escape(label)}:</strong> {escape(value)}</p>"


def _paragraphs(value: str) -> str:
    return "<br>".join(escape(part) for part in value.splitlines())


__all__ = [
    "TranslationDeliveryError",
    "publish_translation_html",
    "validate_translation_html",
]
