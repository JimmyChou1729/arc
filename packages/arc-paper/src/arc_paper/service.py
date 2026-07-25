"""Typed deterministic facade for paper acquisition, import, and parsing.

The service owns no queue, worker, checkpoint, or run state.  Durable workflow
execution belongs to :mod:`arc_jobs`; LLM work belongs to :mod:`arc_llm`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from ._cache_root import resolve_cache_root
from .arxiv_document import (
    ArxivDocumentProvenance,
    ArxivEquationSearch,
    ArxivFullTextSearch,
    ArxivSection,
    ArxivTableOfContents,
)
from .document_search import (
    EquationSearchResult,
    FullTextSearchResult,
    TableOfContentsEntry,
    search_equations as _search_equations,
    search_full_text as _search_full_text,
    select_section as _select_section,
    table_of_contents as _table_of_contents,
)
from .ids import extract_paper_ids as _extract_paper_ids
from .ids import arxiv_path_id
from .ids import paper_ids_safe_dir_name as _paper_ids_safe_dir_name
from .parse import (
    PDFTextExtractor,
    PaperParserService,
    ParsedDocument,
    ParsedSection,
)
from .providers import Ar5ivProvider, ArxivPdfProvider, InspireProvider
from .source_repository import SourceRepository
from .sources import (
    ParseOutcome,
    SourceArtifact,
    SourceBundle,
    SourceFormat,
    ValidationPolicy,
)


class PaperInputError(ValueError):
    """A stable invalid-request error raised before external effects."""

    code = "invalid_request"

    def __init__(self, message: str, *, code: str = "invalid_request"):
        super().__init__(message)
        self.code = code
        self.message = message


def default_cache_root() -> Path:
    return resolve_cache_root()


class ArcPaperService:
    """Small, injectable facade over the package-owned deterministic services."""

    def __init__(
        self,
        *,
        cache_root: str | Path | None = None,
        repository: SourceRepository | None = None,
        inspire: InspireProvider | None = None,
        ar5iv: Ar5ivProvider | None = None,
        arxiv_pdf: ArxivPdfProvider | None = None,
        pdf_text_extractor: PDFTextExtractor | None = None,
    ):
        try:
            root = resolve_cache_root(cache_root, repository=repository)
        except ValueError as exc:
            raise PaperInputError(
                "cache_root must match the injected SourceRepository root"
            ) from exc
        self.repository = repository or SourceRepository(root)
        self.inspire = inspire or InspireProvider(cache_root=root)
        self.ar5iv = ar5iv or Ar5ivProvider(
            cache_root=root, source_repository=self.repository
        )
        self.arxiv_pdf = arxiv_pdf or ArxivPdfProvider(
            cache_root=root, source_repository=self.repository
        )
        self.parser = PaperParserService(
            self.repository,
            pdf_text_extractor=pdf_text_extractor,
        )

    def import_source(
        self,
        path: str | Path,
        *,
        source_format: SourceFormat | str | None = None,
    ) -> SourceArtifact:
        source = self.repository.import_path(path, source_format=source_format)
        self.parser.parse_source(source)
        return source

    def fetch_arxiv_auto(
        self, paper_id: str, *, refresh: bool = False
    ) -> SourceArtifact:
        """Fetch only the ar5iv primary; auto never downloads a PDF."""

        source, _, _ = self._fetch_arxiv_auto_materialized(
            paper_id, refresh=refresh
        )
        return source

    def fetch_arxiv_pdf(
        self, paper_id: str, *, refresh: bool = False
    ) -> SourceArtifact:
        source = self.arxiv_pdf.fetch(paper_id, refresh=refresh)
        self.parser.parse_source(source)
        return source

    def parse_bundle(
        self,
        bundle: SourceBundle,
        *,
        policy: ValidationPolicy | str | None = None,
    ) -> ParseOutcome:
        resolved = ValidationPolicy(policy) if policy is not None else None
        return self.parser.parse(bundle, policy=resolved)

    def parse_local(
        self,
        primary_path: str | Path,
        *,
        validator_paths: Sequence[str | Path] = (),
        primary_format: SourceFormat | str | None = None,
        validator_formats: Sequence[SourceFormat | str | None] = (),
        policy: ValidationPolicy | str | None = None,
    ) -> ParseOutcome:
        if validator_formats and len(validator_formats) != len(validator_paths):
            raise PaperInputError(
                "validator_formats must be empty or match validator_paths"
            )
        primary = self.repository.import_path(
            primary_path, source_format=primary_format
        )
        formats = (
            tuple(validator_formats)
            if validator_formats
            else (None,) * len(validator_paths)
        )
        validators = tuple(
            self.repository.import_path(path, source_format=source_format)
            for path, source_format in zip(validator_paths, formats, strict=True)
        )
        return self.parse_bundle(
            SourceBundle(primary=primary, validators=validators),
            policy=policy,
        )

    def parse_arxiv_auto(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
    ) -> ParseOutcome:
        primary = self.ar5iv.fetch(paper_id, refresh=refresh)
        return self.parse_bundle(SourceBundle(primary=primary))

    def parse_arxiv_pdf(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
    ) -> ParseOutcome:
        primary = self.arxiv_pdf.fetch(paper_id, refresh=refresh)
        return self.parse_bundle(SourceBundle(primary=primary))

    def get_metadata(self, paper_id: str, *, refresh: bool = False) -> dict[str, Any]:
        return self.inspire.get_metadata(_require_paper_id(paper_id), refresh=refresh)

    def get_title(self, paper_id: str, *, refresh: bool = False) -> str:
        return str(self.get_metadata(paper_id, refresh=refresh).get("title") or "")

    def get_abstract(self, paper_id: str, *, refresh: bool = False) -> str:
        return str(self.get_metadata(paper_id, refresh=refresh).get("abstract") or "")

    def get_authors(self, paper_id: str, *, refresh: bool = False) -> list[str]:
        value = self.get_metadata(paper_id, refresh=refresh).get("authors") or []
        return [str(item) for item in value]

    def get_references(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
        enrich: bool = False,
    ) -> list[dict[str, Any]]:
        return self.inspire.get_references(
            _require_paper_id(paper_id), refresh=refresh, enrich=enrich
        )

    def get_citers(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
        limit: int = 1000,
        sort: str = "mostrecent",
    ) -> list[dict[str, Any]]:
        return self.inspire.get_citers(
            _require_paper_id(paper_id),
            refresh=refresh,
            limit=limit,
            sort=sort,
        )

    def get_citer_count(self, paper_id: str, *, refresh: bool = False) -> int:
        return self.inspire.get_citer_count(
            _require_paper_id(paper_id), refresh=refresh
        )

    def search_metadata(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.inspire.search_metadata(query, limit=limit)

    def search_full_text(
        self,
        documents: ParsedDocument | Iterable[ParsedDocument],
        query: str,
        *,
        limit: int = 20,
        context_lines: int = 1,
        case_sensitive: bool = False,
    ) -> FullTextSearchResult:
        return _search_full_text(
            documents,
            query,
            limit=limit,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )

    def search_equations(
        self,
        documents: ParsedDocument | Iterable[ParsedDocument],
        query: str,
        *,
        limit: int = 20,
        case_sensitive: bool = False,
    ) -> EquationSearchResult:
        return _search_equations(
            documents,
            query,
            limit=limit,
            case_sensitive=case_sensitive,
        )

    def table_of_contents(
        self, document: ParsedDocument
    ) -> tuple[TableOfContentsEntry, ...]:
        return _table_of_contents(document)

    def select_section(
        self, document: ParsedDocument, selector: str | int
    ) -> ParsedSection:
        return _select_section(document, selector)

    def get_arxiv_table_of_contents(
        self,
        arxiv_id: str,
        *,
        refresh: bool = False,
    ) -> ArxivTableOfContents:
        document, provenance, warnings = self._resolve_arxiv_document(
            arxiv_id, refresh=refresh
        )
        return ArxivTableOfContents(
            provenance=provenance,
            entries=_table_of_contents(document),
            warnings=warnings,
        )

    def get_arxiv_section(
        self,
        arxiv_id: str,
        selector: str | int,
        *,
        refresh: bool = False,
    ) -> ArxivSection:
        document, provenance, warnings = self._resolve_arxiv_document(
            arxiv_id, refresh=refresh
        )
        section = _select_section(document, selector)
        return ArxivSection(
            provenance=provenance,
            section_id=section.section_id,
            title=section.title,
            text=section.text,
            level=section.level,
            ordinal=section.ordinal,
            page_start=section.page_start,
            page_end=section.page_end,
            warnings=warnings,
        )

    def search_arxiv_full_text(
        self,
        arxiv_id: str,
        query: str,
        *,
        limit: int = 20,
        context_lines: int = 1,
        case_sensitive: bool = False,
        refresh: bool = False,
    ) -> ArxivFullTextSearch:
        document, provenance, warnings = self._resolve_arxiv_document(
            arxiv_id, refresh=refresh
        )
        result = _search_full_text(
            document,
            query,
            limit=limit,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )
        return ArxivFullTextSearch(
            provenance=provenance,
            query=result.query,
            matches=result.matches,
            limit=result.limit,
            context_lines=result.context_lines,
            case_sensitive=result.case_sensitive,
            truncated=result.truncated,
            warnings=warnings,
        )

    def search_arxiv_equations(
        self,
        arxiv_id: str,
        query: str,
        *,
        limit: int = 20,
        case_sensitive: bool = False,
        refresh: bool = False,
    ) -> ArxivEquationSearch:
        document, provenance, warnings = self._resolve_arxiv_document(
            arxiv_id, refresh=refresh
        )
        result = _search_equations(
            document,
            query,
            limit=limit,
            case_sensitive=case_sensitive,
        )
        return ArxivEquationSearch(
            provenance=provenance,
            query=result.query,
            matches=result.matches,
            limit=result.limit,
            case_sensitive=result.case_sensitive,
            truncated=result.truncated,
            warnings=warnings,
        )

    def _resolve_arxiv_document(
        self,
        arxiv_id: str,
        *,
        refresh: bool,
    ) -> tuple[
        ParsedDocument,
        ArxivDocumentProvenance,
        tuple[str, ...],
    ]:
        path_id = arxiv_path_id(str(arxiv_id or ""))
        if not path_id:
            raise PaperInputError(
                f"arXiv document operation requires an arXiv ID: {arxiv_id}",
                code="not_arxiv_id",
            )
        canonical_id = f"arXiv:{path_id}"
        source, document, warnings = self._fetch_arxiv_auto_materialized(
            canonical_id, refresh=refresh
        )
        provenance = ArxivDocumentProvenance(
            canonical_arxiv_id=canonical_id,
            provider="ar5iv",
            source_format=source.source_format.value,
            source_digest=source.artifact_digest,
            document_digest=document.document_digest,
        )
        return document, provenance, warnings

    def _fetch_arxiv_auto_materialized(
        self, paper_id: str, *, refresh: bool
    ) -> tuple[
        SourceArtifact,
        ParsedDocument,
        tuple[str, ...],
    ]:
        source = self.ar5iv.fetch(paper_id, refresh=refresh)
        document, warnings = self.parser.materialize_source(source)
        return source, document, warnings


def extract_paper_ids(text: str) -> list[str]:
    return _extract_paper_ids(str(text))


def paper_ids_safe_dir_name(ids: str | Iterable[str]) -> str:
    values = [ids] if isinstance(ids, str) else list(ids)
    normalized = [str(item) for item in values if str(item).strip()]
    if not normalized:
        raise PaperInputError("at least one paper id is required")
    return _paper_ids_safe_dir_name(normalized)


def _require_paper_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise PaperInputError("paper_id is required")
    return normalized


def get_metadata(paper_id: str, *, refresh: bool = False) -> dict[str, Any]:
    return ArcPaperService().get_metadata(paper_id, refresh=refresh)


def get_title(paper_id: str, *, refresh: bool = False) -> str:
    return ArcPaperService().get_title(paper_id, refresh=refresh)


def get_abstract(paper_id: str, *, refresh: bool = False) -> str:
    return ArcPaperService().get_abstract(paper_id, refresh=refresh)


def get_authors(paper_id: str, *, refresh: bool = False) -> list[str]:
    return ArcPaperService().get_authors(paper_id, refresh=refresh)


def get_references(
    paper_id: str, *, refresh: bool = False, enrich: bool = False
) -> list[dict[str, Any]]:
    return ArcPaperService().get_references(
        paper_id, refresh=refresh, enrich=enrich
    )


def get_citers(
    paper_id: str,
    *,
    refresh: bool = False,
    limit: int = 1000,
    sort: str = "mostrecent",
) -> list[dict[str, Any]]:
    return ArcPaperService().get_citers(
        paper_id, refresh=refresh, limit=limit, sort=sort
    )


def get_citer_count(paper_id: str, *, refresh: bool = False) -> int:
    return ArcPaperService().get_citer_count(paper_id, refresh=refresh)


def search_metadata(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return ArcPaperService().search_metadata(query, limit=limit)


def search_full_text(
    documents: ParsedDocument | Iterable[ParsedDocument],
    query: str,
    *,
    limit: int = 20,
    context_lines: int = 1,
    case_sensitive: bool = False,
) -> FullTextSearchResult:
    return _search_full_text(
        documents,
        query,
        limit=limit,
        context_lines=context_lines,
        case_sensitive=case_sensitive,
    )


def search_equations(
    documents: ParsedDocument | Iterable[ParsedDocument],
    query: str,
    *,
    limit: int = 20,
    case_sensitive: bool = False,
) -> EquationSearchResult:
    return _search_equations(
        documents,
        query,
        limit=limit,
        case_sensitive=case_sensitive,
    )


def table_of_contents(
    document: ParsedDocument,
) -> tuple[TableOfContentsEntry, ...]:
    return _table_of_contents(document)


def select_section(
    document: ParsedDocument, selector: str | int
) -> ParsedSection:
    return _select_section(document, selector)


def get_arxiv_table_of_contents(
    arxiv_id: str,
    *,
    refresh: bool = False,
) -> ArxivTableOfContents:
    return ArcPaperService().get_arxiv_table_of_contents(
        arxiv_id, refresh=refresh
    )


def get_arxiv_section(
    arxiv_id: str,
    selector: str | int,
    *,
    refresh: bool = False,
) -> ArxivSection:
    return ArcPaperService().get_arxiv_section(
        arxiv_id, selector, refresh=refresh
    )


def search_arxiv_full_text(
    arxiv_id: str,
    query: str,
    *,
    limit: int = 20,
    context_lines: int = 1,
    case_sensitive: bool = False,
    refresh: bool = False,
) -> ArxivFullTextSearch:
    return ArcPaperService().search_arxiv_full_text(
        arxiv_id,
        query,
        limit=limit,
        context_lines=context_lines,
        case_sensitive=case_sensitive,
        refresh=refresh,
    )


def search_arxiv_equations(
    arxiv_id: str,
    query: str,
    *,
    limit: int = 20,
    case_sensitive: bool = False,
    refresh: bool = False,
) -> ArxivEquationSearch:
    return ArcPaperService().search_arxiv_equations(
        arxiv_id,
        query,
        limit=limit,
        case_sensitive=case_sensitive,
        refresh=refresh,
    )


def import_source(
    path: str | Path,
    *,
    cache_root: str | Path | None = None,
    source_format: SourceFormat | str | None = None,
) -> SourceArtifact:
    return ArcPaperService(cache_root=cache_root).import_source(
        path, source_format=source_format
    )


def fetch_arxiv_auto(
    paper_id: str,
    *,
    cache_root: str | Path | None = None,
    refresh: bool = False,
) -> SourceArtifact:
    return ArcPaperService(cache_root=cache_root).fetch_arxiv_auto(
        paper_id, refresh=refresh
    )


def fetch_arxiv_pdf(
    paper_id: str,
    *,
    cache_root: str | Path | None = None,
    refresh: bool = False,
) -> SourceArtifact:
    return ArcPaperService(cache_root=cache_root).fetch_arxiv_pdf(
        paper_id, refresh=refresh
    )


def parse_local(
    primary_path: str | Path,
    *,
    validator_paths: Sequence[str | Path] = (),
    cache_root: str | Path | None = None,
    primary_format: SourceFormat | str | None = None,
    validator_formats: Sequence[SourceFormat | str | None] = (),
    policy: ValidationPolicy | str | None = None,
) -> ParseOutcome:
    return ArcPaperService(cache_root=cache_root).parse_local(
        primary_path,
        validator_paths=validator_paths,
        primary_format=primary_format,
        validator_formats=validator_formats,
        policy=policy,
    )


__all__ = [
    "ArcPaperService",
    "PaperInputError",
    "default_cache_root",
    "extract_paper_ids",
    "fetch_arxiv_auto",
    "fetch_arxiv_pdf",
    "get_abstract",
    "get_arxiv_section",
    "get_arxiv_table_of_contents",
    "get_authors",
    "get_citer_count",
    "get_citers",
    "get_metadata",
    "get_references",
    "get_title",
    "import_source",
    "paper_ids_safe_dir_name",
    "parse_local",
    "select_section",
    "search_equations",
    "search_arxiv_equations",
    "search_arxiv_full_text",
    "search_full_text",
    "search_metadata",
    "table_of_contents",
]
