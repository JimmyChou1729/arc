"""Cache-first, provider-neutral reference acquisition and local admission."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlparse

import httpx

from .epub import (
    EPUB_MEDIA_TYPE,
    EPUB_READABLE_MEDIA_TYPE,
    derive_epub_readable_html,
)
from .ids import arxiv_path_id, doi_value, normalize_paper_id
from .providers.arxiv_html import ArxivHtmlProvider
from .providers.arxiv_pdf import ArxivPdfProvider
from .providers.base import ProviderError
from .providers.crossref import CrossrefProvider
from .providers.http import HttpResourceProvider
from .providers.inspire import InspireProvider
from .reference_cache import (
    CachedReferenceMaterial,
    CachedResourceRef,
    ReferenceIdentity,
    ReferenceMaterialCache,
    normalize_reference_url,
)


READABLE_MEDIA_TYPES = {
    "application/xhtml+xml",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/x-tex",
}


class ReferenceAcquisitionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AcquiredReferenceResource:
    """Bytes returned by an authorized caller-supplied acquisition backend."""

    payload: bytes
    media_type: str
    source_locator: str
    filename: str = ""
    identity: ReferenceIdentity | None = None


@runtime_checkable
class ReferenceAcquisitionBackend(Protocol):
    """Minimal extension contract; implementations remain outside arc-paper."""

    def acquire(
        self,
        identity: ReferenceIdentity,
        *,
        refresh: bool = False,
    ) -> AcquiredReferenceResource | None: ...


class ReferenceAcquisitionService:
    """Cache-first built-ins plus admission for already available local files."""

    def __init__(
        self,
        *,
        cache_root: str | Path | None = None,
        cache: ReferenceMaterialCache | None = None,
        client: httpx.Client | None = None,
        inspire: InspireProvider | None = None,
        crossref: CrossrefProvider | None = None,
        arxiv_html: ArxivHtmlProvider | None = None,
        arxiv_pdf: ArxivPdfProvider | None = None,
        http: HttpResourceProvider | None = None,
        backends: Sequence[ReferenceAcquisitionBackend] = (),
    ):
        shared_client = client or httpx.Client(timeout=60.0, follow_redirects=True)
        self.cache = cache or ReferenceMaterialCache(cache_root)
        self.inspire = inspire or InspireProvider(
            cache_root=self.cache.root, client=shared_client
        )
        self.crossref = crossref or CrossrefProvider(
            cache_root=self.cache.root, client=shared_client
        )
        self.arxiv_html = arxiv_html or ArxivHtmlProvider(
            cache_root=self.cache.root, client=shared_client
        )
        self.arxiv_pdf = arxiv_pdf or ArxivPdfProvider(
            cache_root=self.cache.root, client=shared_client
        )
        self.http = http or HttpResourceProvider(client=shared_client)
        self.backends = tuple(backends)

    def lookup_cached_reference(
        self,
        *,
        doi: str | None = None,
        arxiv_id: str | None = None,
        url: str | None = None,
        title: str | None = None,
    ) -> CachedReferenceMaterial | None:
        """Perform exact cache-only lookup without invoking any backend."""

        return self.cache.lookup(
            doi=doi, arxiv_id=arxiv_id, url=url, title=title
        )

    def admit_reference_file(
        self,
        path: str | Path,
        identity: ReferenceIdentity | str,
        *,
        media_type: str | None = None,
    ) -> CachedReferenceMaterial:
        """Admit a local or externally downloaded file into verified storage."""

        source = Path(path)
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise ReferenceAcquisitionError(
                "reference_file_unreadable", f"unable to read reference file: {source}"
            ) from exc
        resolved_media = media_type or _media_type_for_file(source, payload)
        return self.admit_reference_bytes(
            payload,
            _identity_for_query(identity),
            media_type=resolved_media,
            source_locator=str(source),
            filename=source.name,
        )

    def admit_reference_bytes(
        self,
        payload: bytes,
        identity: ReferenceIdentity | str,
        *,
        media_type: str,
        source_locator: str = "",
        filename: str = "",
    ) -> CachedReferenceMaterial:
        resolved_identity = _identity_for_query(identity)
        raw = self.cache.store_resource(
            payload,
            media_type=media_type,
            source_locator=source_locator,
            filename=filename,
        )
        resources = [raw]
        readable: CachedResourceRef | None = (
            raw if raw.media_type in READABLE_MEDIA_TYPES else None
        )
        if raw.media_type == EPUB_MEDIA_TYPE:
            derived = derive_epub_readable_html(payload)
            readable = self.cache.store_resource(
                derived.payload,
                media_type=EPUB_READABLE_MEDIA_TYPE,
                source_locator=f"derived:{raw.resource_sha256}:epub-spine",
                filename=f"{Path(filename).stem or 'reference'}.readable.html",
            )
            resources.append(readable)
            if not resolved_identity.title and derived.title:
                resolved_identity = ReferenceIdentity(
                    arxiv_id=resolved_identity.arxiv_id,
                    dois=resolved_identity.dois,
                    urls=resolved_identity.urls,
                    title=derived.title,
                    inspire_recid=resolved_identity.inspire_recid,
                )
        return self.cache.store_material(
            resolved_identity,
            tuple(resources),
            readable_resource=readable,
        )

    def acquire_reference(
        self,
        identity: ReferenceIdentity | str,
        *,
        refresh: bool = False,
    ) -> CachedReferenceMaterial:
        requested = _identity_for_query(identity)
        if not refresh:
            cached = _lookup_identity(self.cache, requested)
            if cached is not None:
                return cached

        for backend in self.backends:
            result = backend.acquire(requested, refresh=refresh)
            if result is not None:
                if not isinstance(result, AcquiredReferenceResource):
                    raise TypeError(
                        "reference acquisition backend returned an unsupported result"
                    )
                return self.admit_reference_bytes(
                    result.payload,
                    result.identity or requested,
                    media_type=result.media_type,
                    source_locator=result.source_locator,
                    filename=result.filename,
                )

        if requested.arxiv_id:
            metadata = _optional_inspire_metadata(
                self.inspire, f"arXiv:{requested.arxiv_id}", refresh=refresh
            )
            resolved = _identity_from_metadata(requested, metadata)
            return self._acquire_arxiv(resolved, refresh=refresh)
        if requested.dois:
            return self._acquire_doi(requested, refresh=refresh)
        if requested.urls:
            return self._acquire_url(requested, requested.urls[0])
        raise ReferenceAcquisitionError(
            "reference_acquisition_unavailable",
            "exact-title acquisition requires a caller-supplied authorized "
            "backend or local admission",
        )

    def _acquire_arxiv(
        self, identity: ReferenceIdentity, *, refresh: bool
    ) -> CachedReferenceMaterial:
        paper_id = f"arXiv:{identity.arxiv_id}"
        errors: list[ProviderError] = []
        for provider in (self.arxiv_html, self.arxiv_pdf):
            try:
                artifact = provider.fetch(paper_id, refresh=refresh)
            except ProviderError as exc:
                errors.append(exc)
                continue
            payload = provider.cache.source_repository.read_bytes(artifact)
            filename = (
                f"{identity.arxiv_id}.html"
                if artifact.media_type == "text/html"
                else f"{identity.arxiv_id}.pdf"
            )
            return self.admit_reference_bytes(
                payload,
                identity,
                media_type=artifact.media_type,
                source_locator=artifact.origin.locator,
                filename=filename,
            )
        raise ReferenceAcquisitionError(
            "arxiv_acquisition_failed",
            _provider_failures(errors, f"no arXiv representation was available for {paper_id}"),
        )

    def _acquire_doi(
        self, requested: ReferenceIdentity, *, refresh: bool
    ) -> CachedReferenceMaterial:
        doi = requested.dois[0]
        inspire_metadata = _optional_inspire_metadata(
            self.inspire, f"doi:{doi}", refresh=refresh
        )
        crossref_metadata: Mapping[str, Any] = {}
        crossref_error: ProviderError | None = None
        try:
            crossref_metadata = self.crossref.get_metadata(
                f"doi:{doi}", refresh=refresh
            )
        except ProviderError as exc:
            crossref_error = exc
        identity = _identity_from_metadata(requested, inspire_metadata)
        identity = _identity_from_metadata(identity, crossref_metadata)
        if identity.arxiv_id:
            return self._acquire_arxiv(identity, refresh=refresh)

        candidates: list[str] = []
        for item in crossref_metadata.get("links") or []:
            if isinstance(item, Mapping) and item.get("url"):
                candidates.append(str(item["url"]))
        if landing := str(crossref_metadata.get("landing_url") or ""):
            candidates.append(landing)
        candidates.append(f"https://doi.org/{doi}")
        errors: list[ProviderError] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                normalized = normalize_reference_url(candidate)
            except ValueError:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                return self._acquire_url(identity, normalized)
            except ReferenceAcquisitionError as exc:
                if exc.__cause__ and isinstance(exc.__cause__, ProviderError):
                    errors.append(exc.__cause__)
                continue
        if crossref_error is not None:
            errors.append(crossref_error)
        raise ReferenceAcquisitionError(
            "doi_acquisition_failed",
            _provider_failures(errors, f"no usable resource was available for doi:{doi}"),
        )

    def _acquire_url(
        self, identity: ReferenceIdentity, url: str
    ) -> CachedReferenceMaterial:
        try:
            acquired = self.http.fetch(url)
        except ProviderError as exc:
            raise ReferenceAcquisitionError(
                "http_reference_acquisition_failed", exc.message
            ) from exc
        urls = _dedupe(
            (*identity.urls, acquired.requested_url, acquired.resolved_url)
        )
        resolved = ReferenceIdentity(
            arxiv_id=identity.arxiv_id,
            dois=identity.dois,
            urls=urls,
            title=identity.title,
            inspire_recid=identity.inspire_recid,
        )
        return self.admit_reference_bytes(
            acquired.payload,
            resolved,
            media_type=acquired.media_type,
            source_locator=acquired.resolved_url,
            filename=acquired.filename,
        )


def lookup_cached_reference(
    *,
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
    title: str | None = None,
    cache_root: str | Path | None = None,
) -> CachedReferenceMaterial | None:
    return ReferenceMaterialCache(cache_root).lookup(
        doi=doi, arxiv_id=arxiv_id, url=url, title=title
    )


def admit_reference_file(
    path: str | Path,
    identity: ReferenceIdentity | str,
    *,
    media_type: str | None = None,
    cache_root: str | Path | None = None,
) -> CachedReferenceMaterial:
    return ReferenceAcquisitionService(cache_root=cache_root).admit_reference_file(
        path, identity, media_type=media_type
    )


def acquire_reference(
    identity: ReferenceIdentity | str,
    *,
    refresh: bool = False,
    cache_root: str | Path | None = None,
) -> CachedReferenceMaterial:
    return ReferenceAcquisitionService(cache_root=cache_root).acquire_reference(
        identity, refresh=refresh
    )


def _lookup_identity(
    cache: ReferenceMaterialCache, identity: ReferenceIdentity
) -> CachedReferenceMaterial | None:
    if identity.arxiv_id:
        return cache.lookup(arxiv_id=identity.arxiv_id)
    for doi in identity.dois:
        if found := cache.lookup(doi=doi):
            return found
    for url in identity.urls:
        if found := cache.lookup(url=url):
            return found
    if identity.title:
        return cache.lookup(title=identity.title)
    return None


def _identity_for_query(
    value: ReferenceIdentity | str,
) -> ReferenceIdentity:
    if isinstance(value, ReferenceIdentity):
        return value
    text = str(value or "").strip()
    normalized = normalize_paper_id(text)
    if arxiv := arxiv_path_id(normalized):
        return ReferenceIdentity(arxiv_id=arxiv)
    if doi := doi_value(normalized):
        return ReferenceIdentity(dois=(doi,))
    if urlparse(text).scheme.casefold() in {"http", "https"}:
        return ReferenceIdentity(urls=(text,))
    if text:
        return ReferenceIdentity(title=text)
    raise ValueError("reference identity is empty")


def _identity_from_metadata(
    base: ReferenceIdentity,
    metadata: Mapping[str, Any],
) -> ReferenceIdentity:
    dois = list(base.dois)
    raw_dois = metadata.get("dois")
    if isinstance(raw_dois, (list, tuple)):
        dois.extend(str(item) for item in raw_dois)
    elif metadata.get("doi"):
        dois.append(str(metadata["doi"]))
    urls = list(base.urls)
    if landing := str(metadata.get("landing_url") or ""):
        urls.append(landing)
    for item in metadata.get("links") or []:
        if isinstance(item, Mapping) and item.get("url"):
            urls.append(str(item["url"]))
    return ReferenceIdentity(
        arxiv_id=str(metadata.get("arxiv_id") or base.arxiv_id),
        dois=_dedupe(dois),
        urls=_dedupe_valid_urls(urls),
        title=str(metadata.get("title") or base.title),
        inspire_recid=str(metadata.get("inspire_recid") or base.inspire_recid),
    )


def _optional_inspire_metadata(
    provider: InspireProvider,
    paper_id: str,
    *,
    refresh: bool,
) -> Mapping[str, Any]:
    try:
        return provider.get_metadata(paper_id, refresh=refresh)
    except ProviderError:
        return {}


def _media_type_for_file(path: Path, payload: bytes) -> str:
    if path.suffix.casefold() == ".epub":
        return EPUB_MEDIA_TYPE
    media_type = mimetypes.guess_type(path.name)[0]
    if media_type:
        return media_type
    if payload.startswith(b"PK\x03\x04"):
        return "application/zip"
    return "application/octet-stream"


def _dedupe(values: Any) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value)
        if item.casefold() not in seen:
            seen.add(item.casefold())
            out.append(item)
    return tuple(out)


def _dedupe_valid_urls(values: Any) -> tuple[str, ...]:
    normalized = []
    for value in values:
        try:
            normalized.append(normalize_reference_url(str(value)))
        except ValueError:
            continue
    return _dedupe(normalized)


def _provider_failures(errors: list[ProviderError], fallback: str) -> str:
    if not errors:
        return fallback
    return "; ".join(f"{item.code}: {item.message}" for item in errors)


__all__ = [
    "AcquiredReferenceResource",
    "ReferenceAcquisitionBackend",
    "ReferenceAcquisitionError",
    "ReferenceAcquisitionService",
    "acquire_reference",
    "admit_reference_file",
    "lookup_cached_reference",
]
