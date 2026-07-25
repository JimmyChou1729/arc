from __future__ import annotations

from pathlib import Path

import httpx

from ..ids import arxiv_path_id
from ..source_repository import SourceRepository
from ..sources import SourceArtifact, SourceFormat, SourceOrigin, SourceOriginKind
from ._http import require_https_host, response_media_type, validate_response_size
from ._request_gate import HostRequestGate, shared_host_gate
from .base import ProviderError
from .remote_cache import RemoteRequestCache


ARXIV_HOST = "arxiv.org"
ARXIV_PDF_MEDIA_TYPE = "application/pdf"
MAX_PDF_BYTES = 250 * 1024 * 1024


def arxiv_pdf_url(paper_id: str) -> str:
    aid = arxiv_path_id(paper_id)
    if not aid:
        raise ProviderError(
            "not_arxiv_id", f"arXiv PDF requires an arXiv ID: {paper_id}"
        )
    return f"https://{ARXIV_HOST}/pdf/{aid}"


class ArxivPdfProvider:
    """Explicit arXiv PDF acquisition.

    Nothing in the ar5iv provider calls this provider; callers must request a
    PDF directly or include it explicitly as a validator.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        cache_root: str | Path | None = None,
        source_repository: SourceRepository | None = None,
        request_cache: RemoteRequestCache | None = None,
        request_gate: HostRequestGate | None = None,
    ):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.timeout = timeout
        self.cache = request_cache or RemoteRequestCache(
            cache_root, source_repository=source_repository
        )
        self.request_gate = request_gate or shared_host_gate(
            self.cache.root, ARXIV_HOST
        )

    def fetch(self, paper_id: str, *, refresh: bool = False) -> SourceArtifact:
        aid = arxiv_path_id(paper_id)
        url = arxiv_pdf_url(paper_id)
        origin = SourceOrigin(
            kind=SourceOriginKind.REMOTE_PROVIDER,
            provider="arxiv-pdf",
            locator=url,
            metadata={"arxiv_id": aid},
        )
        return self.cache.fetch_source(
            "arxiv-pdf",
            aid,
            source_format=SourceFormat.PDF,
            media_type=ARXIV_PDF_MEDIA_TYPE,
            origin=origin,
            refresh=refresh,
            fetch=lambda: self._fetch_pdf_bytes(url, paper_id),
        )

    def _fetch_pdf_bytes(self, url: str, paper_id: str) -> bytes:
        require_https_host(url, ARXIV_HOST)
        response = self.request_gate.request(
            lambda: self.client.get(url, timeout=self.timeout)
        )
        if response.status_code == 404:
            raise ProviderError(
                "arxiv_pdf_not_found", f"arXiv PDF not found for {paper_id}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "arxiv_pdf_fetch_failed",
                str(exc),
                status_code=exc.response.status_code,
            ) from exc
        require_https_host(str(response.url), ARXIV_HOST)
        validate_response_size(response, MAX_PDF_BYTES, "arxiv_pdf_too_large")
        media_type = response_media_type(response)
        if media_type != ARXIV_PDF_MEDIA_TYPE:
            raise ProviderError(
                "arxiv_pdf_media_type_invalid",
                f"arXiv returned unsupported media type: {media_type or '<missing>'}",
            )
        payload = bytes(response.content)
        if not payload.startswith(b"%PDF-"):
            raise ProviderError(
                "arxiv_pdf_invalid", "arXiv response is not a PDF document"
            )
        return payload


__all__ = [
    "ARXIV_PDF_MEDIA_TYPE",
    "ArxivPdfProvider",
    "MAX_PDF_BYTES",
    "arxiv_pdf_url",
]
