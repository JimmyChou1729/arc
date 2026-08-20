"""Paper-aware rich-document parser specialization."""

from arc_document.rich_document.service import *  # noqa: F401,F403
from arc_document.rich_document.service import (
    RichDocumentParserService as _DocumentRichDocumentParserService,
)

from ..parse import PaperParserService


class RichDocumentParserService(_DocumentRichDocumentParserService):
    """Use paper corpus indexing with the neutral rich parser."""

    def __init__(
        self,
        repository,
        *,
        pdf_text_extractor=None,
        asset_importer=None,
    ):
        super().__init__(
            repository,
            pdf_text_extractor=pdf_text_extractor,
            asset_importer=asset_importer,
        )
        self.standard_parser = PaperParserService(
            repository, pdf_text_extractor=pdf_text_extractor
        )
