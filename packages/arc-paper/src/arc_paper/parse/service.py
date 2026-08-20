"""Paper-aware parser specialization over :mod:`arc_document`."""

from arc_document.parse.service import DocumentParserService

from .._full_text_catalog import FullTextCatalog


class PaperParserService(DocumentParserService):
    """Preserve paper-corpus indexing while reusing document parsing."""

    def __init__(self, repository, *, pdf_text_extractor=None):
        super().__init__(
            repository, pdf_text_extractor=pdf_text_extractor
        )
        self._paper_catalog = FullTextCatalog(repository.root)

    def materialize_source(self, artifact):
        document, warnings = super().materialize_source(artifact)
        parser_contract = self.parser_contract_for(artifact)
        cache = self._caches[parser_contract]
        self._paper_catalog.record(
            artifact,
            document,
            parser_contract=parser_contract,
            parsed_cache_key=cache.cache_key(artifact),
        )
        return document, warnings


__all__ = ["DocumentParserService", "PaperParserService"]
