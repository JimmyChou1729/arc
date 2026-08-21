"""Compatibility rich-document parser names."""

from arc_document import RichDocumentParserService as _DocumentRichDocumentParserService


class RichDocumentParserService(_DocumentRichDocumentParserService):
    """Backward-compatible name for the document-owned rich parser."""


__all__ = ["RichDocumentParserService"]
