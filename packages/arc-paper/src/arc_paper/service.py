"""Typed facade for deterministic paper access and explicit workflows.

Deterministic methods own no run state. ``extract_keywords`` is a convenience
wrapper over the package's explicit :mod:`arc_jobs` workflow; LLM execution
remains owned by :mod:`arc_llm`.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arc_jobs import JsonValue, RunStatus
from arc_llm import HostAuthority, LLMExecutionOptions, ModelSelection

from ._cache_admin import (
    CacheAdministrator,
    CacheListResult,
    CacheRemoveResult,
    CacheUpdateRecord,
    CacheUpdateResult,
    PaperCacheIndex,
)
from ._cache_root import resolve_cache_root
from .arxiv_document import (
    ArxivDocumentProvenance,
    ArxivEquationSearch,
    ArxivFullTextSearch,
    ArxivSection,
    ArxivTableOfContents,
)
from .cached_full_text_search import (
    CachedFullTextSearchResult,
    CachedFullTextSearcher,
)
from .cached_document import (
    CachedDocumentError,
    CachedDocumentRef,
    CachedDocumentSearch,
    CachedSection,
    CachedSourceRange,
    CachedTableOfContents,
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
from .ids import arxiv_path_id, normalize_paper_id
from .ids import extract_paper_ids as _extract_paper_ids
from .ids import paper_ids_safe_dir_name as _paper_ids_safe_dir_name
from .parse import (
    PDFTextExtractor,
    PaperParserService,
    ParsedDocument,
    ParsedSection,
)
from .providers import (
    Ar5ivProvider,
    ArxivHtmlProvider,
    ArxivPdfProvider,
    InspireProvider,
)
from .providers.base import ProviderError
from .source_repository import SourceRepository
from .sources import (
    ParseOutcome,
    SourceArtifact,
    SourceBundle,
    SourceFormat,
    ValidationPolicy,
)

if TYPE_CHECKING:
    from .rich_document import RichDocument
    from .terms import KeywordResult, TermInventoryStore


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
    """Injectable facade over package-owned deterministic and workflow services."""

    def __init__(
        self,
        *,
        cache_root: str | Path | None = None,
        repository: SourceRepository | None = None,
        inspire: InspireProvider | None = None,
        arxiv_html: ArxivHtmlProvider | None = None,
        ar5iv: Ar5ivProvider | None = None,
        arxiv_pdf: ArxivPdfProvider | None = None,
        pdf_text_extractor: PDFTextExtractor | None = None,
        keyword_task_service: Any | None = None,
    ):
        try:
            root = resolve_cache_root(cache_root, repository=repository)
        except ValueError as exc:
            raise PaperInputError(
                "cache_root must match the injected SourceRepository root"
            ) from exc
        self.repository = repository or SourceRepository(root)
        self.cache_root = root
        self.cache_index = PaperCacheIndex(root)
        self.cache_administrator = CacheAdministrator(root)
        self.inspire = inspire or InspireProvider(cache_root=root)
        self.arxiv_html = arxiv_html or ArxivHtmlProvider(
            cache_root=root, source_repository=self.repository
        )
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
        self._cached_full_text_searcher = CachedFullTextSearcher(root)
        self._keyword_task_service = keyword_task_service
        self._term_inventory_store: Any | None = None

    def import_source(
        self,
        path: str | Path,
        *,
        source_format: SourceFormat | str | None = None,
    ) -> SourceArtifact:
        source = self.repository.import_path(path, source_format=source_format)
        self.parser.parse_source(source)
        self._record_local_source(source)
        return source

    def fetch_arxiv_auto(
        self, paper_id: str, *, refresh: bool = False
    ) -> SourceArtifact:
        """Fetch official arXiv HTML, falling back to ar5iv only on a 404."""

        source, _, _ = self._fetch_arxiv_auto_materialized(
            paper_id, refresh=refresh
        )
        return source

    def fetch_arxiv_pdf(
        self, paper_id: str, *, refresh: bool = False
    ) -> SourceArtifact:
        source = self.arxiv_pdf.fetch(paper_id, refresh=refresh)
        self.parser.parse_source(source)
        self._record_remote_component(
            paper_id,
            "arxiv-pdf",
            cache=getattr(self.arxiv_pdf, "cache", None),
            kind="source",
            namespace="arxiv-pdf",
            request_key=arxiv_path_id(paper_id),
        )
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
        outcome = self.parse_bundle(
            SourceBundle(primary=primary, validators=validators),
            policy=policy,
        )
        self._record_local_source(primary)
        for validator in validators:
            self._record_local_source(validator)
        return outcome

    def parse_arxiv_auto(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
    ) -> ParseOutcome:
        primary = self._fetch_arxiv_auto_source(paper_id, refresh=refresh)
        outcome = self.parse_bundle(SourceBundle(primary=primary))
        self._record_arxiv_auto_component(paper_id, primary)
        return outcome

    def parse_arxiv_pdf(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
    ) -> ParseOutcome:
        primary = self.arxiv_pdf.fetch(paper_id, refresh=refresh)
        outcome = self.parse_bundle(SourceBundle(primary=primary))
        self._record_remote_component(
            paper_id,
            "arxiv-pdf",
            cache=getattr(self.arxiv_pdf, "cache", None),
            kind="source",
            namespace="arxiv-pdf",
            request_key=arxiv_path_id(paper_id),
        )
        return outcome

    def get_metadata(self, paper_id: str, *, refresh: bool = False) -> dict[str, Any]:
        required = _require_paper_id(paper_id)
        value = self.inspire.get_metadata(required, refresh=refresh)
        self._record_remote_component(
            required,
            "inspire-record",
            cache=getattr(self.inspire, "cache", None),
            kind="json",
            namespace="inspire-record",
            request_key=normalize_paper_id(required),
        )
        return value

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
        required = _require_paper_id(paper_id)
        value = self.inspire.get_references(
            required, refresh=refresh, enrich=enrich
        )
        self._record_remote_component(
            required,
            "inspire-record",
            cache=getattr(self.inspire, "cache", None),
            kind="json",
            namespace="inspire-record",
            request_key=normalize_paper_id(required),
        )
        return value

    def get_citers(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
        limit: int = 1000,
        sort: str = "mostrecent",
    ) -> list[dict[str, Any]]:
        required = _require_paper_id(paper_id)
        value = self.inspire.get_citers(
            required,
            refresh=refresh,
            limit=limit,
            sort=sort,
        )
        try:
            metadata = self.inspire.get_metadata(required, refresh=False)
            recid = str(metadata.get("inspire_recid") or "")
            if recid:
                request_key = json.dumps(
                    {"recid": recid, "sort": sort, "limit": limit},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self._record_remote_component(
                    required,
                    f"inspire-citers:{sort}:{limit}",
                    cache=getattr(self.inspire, "cache", None),
                    kind="json",
                    namespace="inspire-citers",
                    request_key=request_key,
                )
        except Exception:
            # The citer result remains valid even if optional admin indexing fails.
            pass
        return value

    def get_citer_count(self, paper_id: str, *, refresh: bool = False) -> int:
        return self.inspire.get_citer_count(
            _require_paper_id(paper_id), refresh=refresh
        )

    def search_citers(
        self,
        paper_id: str,
        terms: Sequence[str],
        *,
        refresh: bool = False,
        scan_limit: int = 1000,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Shortlist direct citers by normalized literal title/abstract terms."""

        required = _require_paper_id(paper_id)
        normalized_terms = _require_citer_search_terms(terms)
        scan_limit = _require_limit(
            scan_limit, name="scan_limit", maximum=1000
        )
        limit = _require_limit(limit, name="limit", maximum=50)
        total_citer_count = self.get_citer_count(required, refresh=refresh)

        if total_citer_count == 0:
            scanned: list[dict[str, Any]] = []
            scan_strategy = "all-mostrecent"
        elif total_citer_count <= scan_limit:
            scanned = [
                dict(record)
                for record in self.get_citers(
                    required,
                    refresh=refresh,
                    limit=scan_limit,
                    sort="mostrecent",
                )
            ]
            scan_strategy = "all-mostrecent"
        else:
            recent_limit = (scan_limit + 1) // 2
            cited_limit = scan_limit // 2
            recent = self.get_citers(
                required,
                refresh=refresh,
                limit=recent_limit,
                sort="mostrecent",
            )
            cited = (
                self.get_citers(
                    required,
                    refresh=refresh,
                    limit=cited_limit,
                    sort="mostcited",
                )
                if cited_limit
                else []
            )
            scanned = _dedupe_citer_records((*recent, *cited))
            scan_strategy = "split-mostrecent-mostcited"

        matches = _match_citer_records(scanned, normalized_terms)
        returned_matches = matches[:limit]
        scanned_count = len(scanned)
        scan_complete = (
            total_citer_count <= scan_limit
            and scanned_count >= total_citer_count
        )
        return {
            "paper_id": normalize_paper_id(required),
            "total_citer_count": total_citer_count,
            "scanned_count": scanned_count,
            "scan_complete": scan_complete,
            "scan_strategy": scan_strategy,
            "terms": [term for term, _ in normalized_terms],
            "matched_count": len(matches),
            "returned_count": len(returned_matches),
            "matches_truncated": len(matches) > len(returned_matches),
            "matches": returned_matches,
            "control_sample": _citer_control_sample(scanned),
        }

    def search_metadata(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        return self.inspire.search_metadata(query, limit=limit)

    def list_cache(
        self,
        *,
        paper_ids: Sequence[str] = (),
        entry_ids: Sequence[str] = (),
        since_seconds: int | None = None,
    ) -> CacheListResult:
        return self.cache_administrator.list(
            paper_ids=paper_ids,
            entry_ids=entry_ids,
            since_seconds=since_seconds,
        )

    def remove_cache(
        self,
        *,
        paper_ids: Sequence[str] = (),
        entry_ids: Sequence[str] = (),
        dry_run: bool = True,
    ) -> CacheRemoveResult:
        if not paper_ids and not entry_ids:
            raise PaperInputError(
                "cache remove requires at least one exact paper or entry id"
            )
        selected = self.cache_administrator.list(
            paper_ids=paper_ids,
            entry_ids=entry_ids,
        ).entries
        if dry_run:
            return CacheRemoveResult(True, selected, ())

        removed: list[str] = []
        warnings: list[str] = []
        for entry in selected:
            changed = False
            changed = (
                self.cache_administrator.term_inventory.remove_admin_entry(
                    entry.entry_id
                )
                or changed
            )
            for component in entry.components:
                for storage_entry_id in component.storage_entry_ids:
                    if storage_entry_id.startswith("remote:"):
                        changed = (
                            self.cache_administrator.remote.remove_admin_entry(
                                storage_entry_id
                            )
                            or changed
                        )
            changed = (
                self.cache_administrator.catalog.remove_admin_entry(entry.entry_id)
                or changed
            )
            changed = self.cache_index.remove(entry.entry_id) or changed
            if changed:
                removed.append(entry.entry_id)
            else:
                warnings.append(f"cache_entry_already_absent:{entry.entry_id}")
        return CacheRemoveResult(
            False,
            selected,
            tuple(removed),
            warnings=tuple(warnings),
        )

    def update_cache(
        self,
        *,
        paper_ids: Sequence[str] = (),
        entry_ids: Sequence[str] = (),
    ) -> CacheUpdateResult:
        if not paper_ids and not entry_ids:
            raise PaperInputError(
                "cache update requires at least one exact paper or entry id"
            )
        selected = self.cache_administrator.list(
            paper_ids=paper_ids,
            entry_ids=entry_ids,
        ).entries
        records: list[CacheUpdateRecord] = []
        warnings: list[str] = []
        for entry in selected:
            if entry.kind != "paper" or entry.paper_id is None:
                records.append(
                    CacheUpdateRecord(
                        entry.entry_id,
                        "all",
                        "skipped",
                        "only paper cache entries can be refreshed",
                    )
                )
                continue
            paper_id = entry.paper_id
            metadata: dict[str, Any] | None = None
            try:
                metadata = self.get_metadata(paper_id, refresh=True)
                records.append(
                    CacheUpdateRecord(entry.entry_id, "inspire-record", "updated")
                )
            except Exception as exc:
                records.append(
                    CacheUpdateRecord(
                        entry.entry_id,
                        "inspire-record",
                        "failed",
                        _cache_error_message(exc),
                    )
                )
            for sort in ("mostrecent", "mostcited"):
                component = f"inspire-citers:{sort}:1000"
                try:
                    self.get_citers(
                        paper_id,
                        refresh=True,
                        limit=1000,
                        sort=sort,
                    )
                    records.append(
                        CacheUpdateRecord(entry.entry_id, component, "updated")
                    )
                except Exception as exc:
                    records.append(
                        CacheUpdateRecord(
                            entry.entry_id,
                            component,
                            "failed",
                            _cache_error_message(exc),
                        )
                    )
            arxiv_id = (
                str((metadata or {}).get("arxiv_id") or "")
                or arxiv_path_id(paper_id)
            )
            arxiv_paper = f"arXiv:{arxiv_id}" if arxiv_id else paper_id
            for component, action in (
                ("arxiv-auto", self.parse_arxiv_auto),
                ("arxiv-pdf", self.parse_arxiv_pdf),
            ):
                try:
                    result = action(arxiv_paper, refresh=True)
                    if component == "arxiv-auto":
                        component = _auto_html_component(result.report.primary)
                    records.append(
                        CacheUpdateRecord(entry.entry_id, component, "updated")
                    )
                except Exception as exc:
                    records.append(
                        CacheUpdateRecord(
                            entry.entry_id,
                            component,
                            "failed",
                            _cache_error_message(exc),
                        )
                    )
        if not selected:
            warnings.append("cache_entry_not_found")
        warnings.extend(
            f"cache_update_failed:{item.entry_id}:{item.component}"
            for item in records
            if item.status == "failed"
        )
        return CacheUpdateResult(tuple(records), tuple(warnings))

    @property
    def term_inventory_store(self) -> TermInventoryStore:
        """Return the lazily constructed keyword cache component."""

        from .terms import TermInventoryStore

        if self._term_inventory_store is None:
            self._term_inventory_store = TermInventoryStore(self.cache_root)
        return self._term_inventory_store

    def extract_keywords(
        self,
        source: SourceArtifact | ParsedDocument | RichDocument,
        *,
        project_dir: str | Path,
        approx_count: int = 50,
        model: ModelSelection = ModelSelection(tier="medium"),
        run_id: str | None = None,
        resume_input: Mapping[str, JsonValue] | None = None,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> KeywordResult:
        """Extract a cache-aware approximate keyword view.

        ``SourceArtifact`` values must belong to this service's repository.
        ``ParsedDocument`` and ``RichDocument`` values cross the semantic seam
        directly and are never re-opened through a path.
        """

        from .rich_document import RichDocument
        from .terms import KeywordResult
        from .workflows.keywords import (
            KeywordExtractionError,
            KeywordExtractionPaused,
            KeywordExtractionRunner,
        )

        if isinstance(source, SourceArtifact):
            document: ParsedDocument | RichDocument = self.parser.parse_source(
                source
            )
        elif isinstance(source, (ParsedDocument, RichDocument)):
            document = source
        else:
            raise PaperInputError(
                "keyword source must be a repository SourceArtifact, ParsedDocument, or RichDocument"
            )
        runner = KeywordExtractionRunner(
            project_dir,
            store=self.term_inventory_store,
            task_service=self._keyword_task_service,
        )
        snapshot = runner.execute(
            document,
            approx_count=approx_count,
            model=model,
            run_id=run_id,
            resume_input=resume_input,
            options=options,
        )
        if snapshot.status is RunStatus.SUCCEEDED:
            result: KeywordResult = runner.read_result(snapshot)
            return result
        if snapshot.status is RunStatus.PAUSED:
            raise KeywordExtractionPaused(snapshot)
        if snapshot.status is RunStatus.FAILED and snapshot.error is not None:
            raise KeywordExtractionError(
                snapshot.error.code, snapshot.error.message
            )
        raise KeywordExtractionError(
            "keyword_extraction_incomplete",
            "keyword extraction ended without a terminal result",
        )

    def search_cached_full_text(
        self,
        terms: Sequence[str],
        *,
        limit: int = 100,
        context_lines: int = 0,
        case_sensitive: bool = False,
    ) -> CachedFullTextSearchResult:
        return self._cached_full_text_searcher.search(
            terms,
            limit=limit,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )

    def cache_document(
        self, source: SourceArtifact | ParsedDocument
    ) -> CachedDocumentRef:
        """Return a logical handle for one verified cached document.

        This operation may populate or repair deterministic derived parse data,
        but it never fetches a provider.  The source bytes must already belong
        to this service's content-addressed repository.
        """

        expected_digest: str | None = None
        if isinstance(source, ParsedDocument):
            artifact = source.source
            expected_digest = source.document_digest
        elif isinstance(source, SourceArtifact):
            artifact = source
        else:
            raise PaperInputError(
                "cached document source must be a SourceArtifact or ParsedDocument"
            )
        document, _ = self.parser.materialize_source(artifact)
        if (
            expected_digest is not None
            and document.document_digest != expected_digest
        ):
            raise CachedDocumentError(
                "cached_document_digest_mismatch",
                "parsed document does not match the verified cached projection",
            )
        return self._cached_document_ref(artifact, document)

    def get_cached_table_of_contents(
        self, document: CachedDocumentRef
    ) -> CachedTableOfContents:
        parsed, warnings = self._resolve_cached_document(document)
        return CachedTableOfContents(
            document=document,
            entries=_table_of_contents(parsed),
            warnings=warnings,
        )

    def get_cached_section(
        self,
        document: CachedDocumentRef,
        selector: str | int,
    ) -> CachedSection:
        parsed, warnings = self._resolve_cached_document(document)
        section = _select_section(parsed, selector)
        return CachedSection(
            document=document,
            section_id=section.section_id,
            title=section.title,
            text=section.text,
            level=section.level,
            ordinal=section.ordinal,
            page_start=section.page_start,
            page_end=section.page_end,
            warnings=warnings,
        )

    def read_cached_source_range(
        self,
        document: CachedDocumentRef,
        start_line: int,
        end_line: int,
    ) -> CachedSourceRange:
        parsed, _ = self._resolve_cached_document(document)
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
        ):
            raise CachedDocumentError(
                "invalid_source_range",
                "source range requires one-based start_line <= end_line",
            )
        if parsed.source.source_format is SourceFormat.PDF:
            raise CachedDocumentError(
                "cached_source_not_text",
                "raw source ranges are unavailable for PDF sources",
            )
        payload = self.repository.read_bytes(parsed.source)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CachedDocumentError(
                "cached_source_not_utf8",
                "cached source is not valid UTF-8 text",
            ) from exc
        lines = text.splitlines()
        total_lines = len(lines)
        if end_line > total_lines:
            raise CachedDocumentError(
                "source_range_out_of_bounds",
                f"source range ends at {end_line}, but source has {total_lines} lines",
            )
        return CachedSourceRange(
            document=document,
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            text="\n".join(lines[start_line - 1 : end_line]),
        )

    def search_cached_document(
        self,
        document: CachedDocumentRef,
        query: str,
        *,
        limit: int = 20,
        context_lines: int = 1,
        case_sensitive: bool = False,
    ) -> CachedDocumentSearch:
        parsed, warnings = self._resolve_cached_document(document)
        result = _search_full_text(
            parsed,
            query,
            limit=limit,
            context_lines=context_lines,
            case_sensitive=case_sensitive,
        )
        return CachedDocumentSearch(
            document=document,
            query=result.query,
            matches=result.matches,
            limit=result.limit,
            context_lines=result.context_lines,
            case_sensitive=result.case_sensitive,
            truncated=result.truncated,
            warnings=warnings,
        )

    def _resolve_cached_document(
        self, reference: CachedDocumentRef
    ) -> tuple[ParsedDocument, tuple[str, ...]]:
        if not isinstance(reference, CachedDocumentRef):
            raise CachedDocumentError(
                "invalid_cached_document_ref",
                "document must be a CachedDocumentRef",
            )
        source = self.repository.get(
            reference.source_format,
            reference.source_sha256,
        )
        if (
            source.size != reference.source_size
            or source.media_type != reference.media_type
        ):
            raise CachedDocumentError(
                "cached_document_source_mismatch",
                "cached source metadata does not match the document reference",
            )
        parser_contract = self.parser.parser_contract_for(source)
        if parser_contract != reference.parser_contract:
            raise CachedDocumentError(
                "cached_document_parser_contract_mismatch",
                "current parser contract does not match the document reference",
            )
        parsed, warnings = self.parser.materialize_source(source)
        if parsed.document_digest != reference.parsed_document_sha256:
            raise CachedDocumentError(
                "cached_document_digest_mismatch",
                "cached parsed document does not match the document reference",
            )
        return parsed, warnings

    def _cached_document_ref(
        self,
        source: SourceArtifact,
        document: ParsedDocument,
    ) -> CachedDocumentRef:
        return CachedDocumentRef(
            source_format=source.source_format,
            source_sha256=source.artifact_digest,
            source_size=source.size,
            media_type=source.media_type,
            parser_contract=self.parser.parser_contract_for(source),
            parsed_document_sha256=document.document_digest,
        )

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
            provider=source.origin.provider,
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
        source = self._fetch_arxiv_auto_source(paper_id, refresh=refresh)
        document, warnings = self.parser.materialize_source(source)
        self._record_arxiv_auto_component(paper_id, source)
        return source, document, warnings

    def _fetch_arxiv_auto_source(
        self, paper_id: str, *, refresh: bool
    ) -> SourceArtifact:
        """Choose the official source, falling back only for an HTML 404."""

        try:
            return self.arxiv_html.fetch(paper_id, refresh=refresh)
        except ProviderError as exc:
            if exc.code != "arxiv_html_not_found":
                raise
        return self.ar5iv.fetch(paper_id, refresh=refresh)

    def _record_arxiv_auto_component(
        self, paper_id: str, source: SourceArtifact
    ) -> None:
        component = _auto_html_component(source)
        provider = self.arxiv_html if component == "arxiv-html" else self.ar5iv
        self._record_remote_component(
            paper_id,
            component,
            cache=getattr(provider, "cache", None),
            kind="source",
            namespace=component,
            request_key=arxiv_path_id(paper_id),
        )

    def _record_remote_component(
        self,
        paper_id: str,
        component: str,
        *,
        cache: Any,
        kind: str,
        namespace: str,
        request_key: str,
    ) -> None:
        if not request_key or not hasattr(cache, "admin_entry"):
            return
        try:
            entry = cache.admin_entry(kind, namespace, request_key)
            if entry is None:
                return
            self.cache_index.record_paper_component(
                paper_id,
                component,
                cached_at=entry.cached_at,
                storage_entry_ids=(entry.entry_id,),
            )
        except (OSError, TypeError, ValueError):
            return

    def _record_local_source(self, source: SourceArtifact) -> None:
        from ._full_text_catalog import FullTextCatalog

        entry_id = (
            f"local:{source.source_format.value}:{source.artifact_digest}"
        )
        try:
            entry = next(
                item
                for item in FullTextCatalog(self.cache_root).admin_entries()
                if item.entry_id == entry_id
            )
            self.cache_index.record_local(
                source,
                cached_at=entry.cached_at,
            )
        except (OSError, StopIteration, TypeError, ValueError):
            return


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


def _require_limit(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaperInputError(f"{name} must be an integer between 1 and {maximum}")
    if not 1 <= value <= maximum:
        raise PaperInputError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _require_citer_search_terms(
    terms: Sequence[str],
) -> list[tuple[str, str]]:
    if isinstance(terms, (str, bytes)):
        raise PaperInputError("terms must be a non-empty sequence of search phrases")
    resolved: list[tuple[str, str]] = []
    seen: set[str] = set()
    for term in terms:
        if not isinstance(term, str):
            raise PaperInputError("citer search terms must be strings")
        surface = term.strip()
        normalized = _normalize_citer_search_text(surface)
        if not normalized:
            raise PaperInputError("citer search terms must not be empty")
        if normalized in seen:
            continue
        seen.add(normalized)
        resolved.append((surface, normalized))
    if not resolved:
        raise PaperInputError("at least one citer search term is required")
    return resolved


def _normalize_citer_search_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    separated = "".join(
        " " if unicodedata.category(character).startswith(("P", "Z")) else character
        for character in folded
    )
    return " ".join(separated.split())


def _citer_identity(record: Mapping[str, Any]) -> str:
    for key in ("paper_id", "inspire_recid", "arxiv_id", "doi"):
        if value := str(record.get(key) or "").strip():
            return f"{key}:{value.casefold()}"
    return "record:" + json.dumps(
        dict(record),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _dedupe_citer_records(
    records: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deduplicated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        identity = _citer_identity(record)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(dict(record))
    return deduplicated


def _match_citer_records(
    records: Sequence[Mapping[str, Any]],
    terms: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for record in records:
        fields = {
            "title": _normalize_citer_search_text(str(record.get("title") or "")),
            "abstract": _normalize_citer_search_text(
                str(record.get("abstract") or "")
            ),
        }
        matched_terms: list[str] = []
        matched_fields: list[str] = []
        for surface, normalized in terms:
            term_fields = [
                field
                for field in ("title", "abstract")
                if normalized in fields[field]
            ]
            if not term_fields:
                continue
            matched_terms.append(surface)
            for field in term_fields:
                if field not in matched_fields:
                    matched_fields.append(field)
        if matched_terms:
            match = dict(record)
            match["matched_terms"] = matched_terms
            match["matched_fields"] = matched_fields
            matches.append(match)

    matches.sort(key=lambda item: str(item.get("paper_id") or ""))
    matches.sort(key=lambda item: str(item.get("published") or ""), reverse=True)
    matches.sort(key=_citer_citation_count, reverse=True)
    matches.sort(key=lambda item: len(item["matched_terms"]), reverse=True)
    matches.sort(key=lambda item: "title" in item["matched_fields"], reverse=True)
    return matches


def _citer_citation_count(record: Mapping[str, Any]) -> int:
    try:
        return int(record.get("citation_count") or 0)
    except (TypeError, ValueError):
        return 0


def _citer_control_sample(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    newest = sorted(records, key=lambda item: str(item.get("paper_id") or ""))
    newest.sort(key=_citer_citation_count, reverse=True)
    newest.sort(key=lambda item: str(item.get("published") or ""), reverse=True)

    most_cited = sorted(records, key=lambda item: str(item.get("paper_id") or ""))
    most_cited.sort(
        key=lambda item: str(item.get("published") or ""), reverse=True
    )
    most_cited.sort(key=_citer_citation_count, reverse=True)

    controls: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for reason, selected in (
        ("newest", newest[:5]),
        ("most-cited", most_cited[:5]),
    ):
        for record in selected:
            identity = _citer_identity(record)
            if identity in positions:
                controls[positions[identity]]["control_reasons"].append(reason)
                continue
            control = dict(record)
            control["control_reasons"] = [reason]
            positions[identity] = len(controls)
            controls.append(control)
    return controls


def _auto_html_component(source: SourceArtifact) -> str:
    provider = source.origin.provider
    if provider in {"arxiv-html", "ar5iv"}:
        return "arxiv-html" if provider == "arxiv-html" else "ar5iv-html"
    raise ProviderError(
        "arxiv_auto_provider_invalid",
        "automatic arXiv HTML source has an unsupported provider",
    )


def _cache_error_message(exc: Exception) -> str:
    code = str(getattr(exc, "code", type(exc).__name__))
    message = str(getattr(exc, "message", str(exc)))
    return f"{code}: {message}"[:500]


def list_cache(
    *,
    paper_ids: Sequence[str] = (),
    entry_ids: Sequence[str] = (),
    since_seconds: int | None = None,
    cache_root: str | Path | None = None,
) -> CacheListResult:
    return ArcPaperService(cache_root=cache_root).list_cache(
        paper_ids=paper_ids,
        entry_ids=entry_ids,
        since_seconds=since_seconds,
    )


def remove_cache(
    *,
    paper_ids: Sequence[str] = (),
    entry_ids: Sequence[str] = (),
    dry_run: bool = True,
    cache_root: str | Path | None = None,
) -> CacheRemoveResult:
    return ArcPaperService(cache_root=cache_root).remove_cache(
        paper_ids=paper_ids,
        entry_ids=entry_ids,
        dry_run=dry_run,
    )


def update_cache(
    *,
    paper_ids: Sequence[str] = (),
    entry_ids: Sequence[str] = (),
    cache_root: str | Path | None = None,
) -> CacheUpdateResult:
    return ArcPaperService(cache_root=cache_root).update_cache(
        paper_ids=paper_ids,
        entry_ids=entry_ids,
    )


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


def search_citers(
    paper_id: str,
    terms: Sequence[str],
    *,
    refresh: bool = False,
    scan_limit: int = 1000,
    limit: int = 50,
) -> dict[str, Any]:
    return ArcPaperService().search_citers(
        paper_id,
        terms,
        refresh=refresh,
        scan_limit=scan_limit,
        limit=limit,
    )


def search_metadata(query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return ArcPaperService().search_metadata(query, limit=limit)


def search_cached_full_text(
    terms: Sequence[str],
    *,
    limit: int = 100,
    context_lines: int = 0,
    case_sensitive: bool = False,
) -> CachedFullTextSearchResult:
    return ArcPaperService().search_cached_full_text(
        terms,
        limit=limit,
        context_lines=context_lines,
        case_sensitive=case_sensitive,
    )


def cache_document(
    source: SourceArtifact | ParsedDocument,
    *,
    cache_root: str | Path | None = None,
) -> CachedDocumentRef:
    return ArcPaperService(cache_root=cache_root).cache_document(source)


def get_cached_table_of_contents(
    document: CachedDocumentRef,
    *,
    cache_root: str | Path | None = None,
) -> CachedTableOfContents:
    return ArcPaperService(cache_root=cache_root).get_cached_table_of_contents(
        document
    )


def get_cached_section(
    document: CachedDocumentRef,
    selector: str | int,
    *,
    cache_root: str | Path | None = None,
) -> CachedSection:
    return ArcPaperService(cache_root=cache_root).get_cached_section(
        document, selector
    )


def read_cached_source_range(
    document: CachedDocumentRef,
    start_line: int,
    end_line: int,
    *,
    cache_root: str | Path | None = None,
) -> CachedSourceRange:
    return ArcPaperService(cache_root=cache_root).read_cached_source_range(
        document, start_line, end_line
    )


def search_cached_document(
    document: CachedDocumentRef,
    query: str,
    *,
    limit: int = 20,
    context_lines: int = 1,
    case_sensitive: bool = False,
    cache_root: str | Path | None = None,
) -> CachedDocumentSearch:
    return ArcPaperService(cache_root=cache_root).search_cached_document(
        document,
        query,
        limit=limit,
        context_lines=context_lines,
        case_sensitive=case_sensitive,
    )


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


def extract_keywords(
    source: str | Path,
    *,
    project_dir: str | Path,
    approx_count: int = 50,
    cache_root: str | Path | None = None,
    refresh: bool = False,
    llm_provider: str = "auto",
    model: str | None = None,
    model_tier: str = "medium",
    run_id: str | None = None,
    resume_input: Mapping[str, JsonValue] | None = None,
    host_authority: str = HostAuthority.UNKNOWN.value,
) -> KeywordResult:
    service = ArcPaperService(cache_root=cache_root)
    source_text = str(source)
    path = Path(source_text)
    artifact = (
        service.import_source(path)
        if path.is_file()
        else service.fetch_arxiv_auto(source_text, refresh=refresh)
    )
    return service.extract_keywords(
        artifact,
        project_dir=project_dir,
        approx_count=approx_count,
        model=ModelSelection(
            provider=llm_provider,
            model=model,
            tier=model_tier,
        ),
        run_id=run_id,
        resume_input=resume_input,
        options=LLMExecutionOptions(host_authority=HostAuthority(host_authority)),
    )


__all__ = [
    "ArcPaperService",
    "CacheListResult",
    "CacheRemoveResult",
    "CacheUpdateRecord",
    "CacheUpdateResult",
    "PaperInputError",
    "default_cache_root",
    "extract_paper_ids",
    "extract_keywords",
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
    "list_cache",
    "paper_ids_safe_dir_name",
    "parse_local",
    "remove_cache",
    "select_section",
    "search_equations",
    "search_arxiv_equations",
    "search_arxiv_full_text",
    "search_cached_full_text",
    "search_citers",
    "search_full_text",
    "search_metadata",
    "table_of_contents",
    "update_cache",
]
