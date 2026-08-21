"""Compatibility parser names over the document-owned implementation."""

from arc_document import DocumentParserService


class PaperParserService(DocumentParserService):
    """Backward-compatible name for :class:`DocumentParserService`."""


__all__ = ["DocumentParserService", "PaperParserService"]
