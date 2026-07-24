"""Cache-first paper acquisition used by the durable domain build.

The domain package deliberately talks to the concrete :class:`ArcPaperService`
facade only.  It does not choose citation providers, fan out work, or maintain
its own paper cache.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from arc_paper import ArcPaperService


CONCLUSION_TEXT_LIMIT = 5_000
_CONCLUSION_TITLES = ("conclusion", "conclusions", "summary")
_FALLBACK_TITLES = ("discussion", "outlook")


class DomainPaperAccess:
    """A narrow, cache-first adapter over one concrete paper-service instance."""

    def __init__(self, paper_service: ArcPaperService | None = None) -> None:
        self._paper_service = paper_service or ArcPaperService()

    def metadata(self, paper_id: str) -> dict[str, Any]:
        """Get cached INSPIRE metadata, propagating an essential failure."""
        return dict(self._paper_service.get_metadata(paper_id, refresh=False))

    def references(self, paper_id: str) -> list[dict[str, Any]]:
        """Get the cached, non-enriched INSPIRE reference list."""
        return list(
            self._paper_service.get_references(
                paper_id,
                refresh=False,
                enrich=False,
            )
        )

    def citers(self, paper_id: str, *, limit: int, sort: str) -> list[dict[str, Any]]:
        """Get one build-selected cached citer ordering without provider options."""
        return list(
            self._paper_service.get_citers(
                paper_id,
                refresh=False,
                limit=limit,
                sort=sort,
            )
        )

    def acquire_pack_record(self, paper_id: str) -> dict[str, Any]:
        """Acquire the pack fields once, keeping nonessential gaps as warnings.

        Metadata and references are deliberately separate calls.  The document
        parse occurs once, and both its table of contents and final-section
        projection use that same parsed document.
        """
        record: dict[str, Any] = {
            "metadata": {},
            "references": [],
            "toc": [],
            "conclusion": None,
            "warnings": [],
        }
        warnings: list[dict[str, Any]] = record["warnings"]

        try:
            record["metadata"] = self.metadata(paper_id)
        except Exception as exc:
            warnings.append(_warning("metadata_unavailable", exc, paper_id))

        try:
            record["references"] = self.references(paper_id)
        except Exception as exc:
            warnings.append(_warning("references_unavailable", exc, paper_id))

        try:
            outcome = self._paper_service.parse_arxiv_auto(paper_id, refresh=False)
            document = outcome.document
            warnings.extend(
                _message_warning("parse_warning", str(message), paper_id)
                for message in getattr(outcome, "warnings", ())
            )
        except Exception as exc:
            warnings.extend(
                [
                    _warning("toc_unavailable", exc, paper_id),
                    _warning("conclusion_section_unavailable", exc, paper_id),
                ]
            )
            return record

        try:
            toc_entries = self._paper_service.table_of_contents(document)
            record["toc"] = _toc_json(toc_entries)
        except Exception as exc:
            warnings.append(_warning("toc_unavailable", exc, paper_id))
            toc_entries = ()

        conclusion_entry = _first_conclusion_entry(toc_entries)
        if conclusion_entry is None:
            warnings.append(
                _message_warning(
                    "conclusion_section_unavailable",
                    "No conclusion, summary, discussion, or outlook section was found.",
                    paper_id,
                )
            )
        else:
            try:
                conclusion = self._paper_service.select_section(
                    document, str(conclusion_entry.section_id)
                )
                record["conclusion"] = _section_json(conclusion)
            except Exception as exc:
                warnings.append(
                    _warning("conclusion_section_unavailable", exc, paper_id)
                )
        return record


def _toc_json(entries: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "section_id": str(entry.section_id),
            "title": str(entry.title),
            "level": int(entry.level),
            "ordinal": int(entry.ordinal),
            "page_start": entry.page_start,
            "page_end": entry.page_end,
        }
        for entry in entries
    ]


def _first_conclusion_entry(entries: Sequence[Any] | Iterable[Any]) -> Any | None:
    values = tuple(entries)
    for title in (*_CONCLUSION_TITLES, *_FALLBACK_TITLES):
        for section in values:
            section_title = getattr(section, "title", "")
            if title in _normalized_title(section_title):
                return section
    return None


def _section_json(section: Any) -> dict[str, str]:
    text = str(getattr(section, "text", ""))
    return {
        "section_id": str(getattr(section, "section_id", "")),
        "title": str(getattr(section, "title", "")),
        "text": text[:CONCLUSION_TEXT_LIMIT],
    }


def _normalized_title(value: Any) -> str:
    return " ".join(str(value).casefold().split())


def _warning(code: str, exc: Exception, paper_id: str) -> dict[str, Any]:
    message = str(exc).strip() or type(exc).__name__
    return _message_warning(code, message, paper_id)


def _message_warning(code: str, message: str, paper_id: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "stage": "paper_acquisition",
        "paper_id": paper_id,
    }


__all__ = ["CONCLUSION_TEXT_LIMIT", "DomainPaperAccess"]
