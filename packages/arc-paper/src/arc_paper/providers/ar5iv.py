from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..ids import arxiv_path_id
from ..source_repository import SourceRepository
from ..sources import SourceArtifact, SourceFormat, SourceOrigin, SourceOriginKind
from .base import ProviderError
from .remote_cache import RemoteRequestCache


AR5IV_HOST = "ar5iv.labs.arxiv.org"
AR5IV_MEDIA_TYPE = "text/html"
MAX_HTML_BYTES = 50 * 1024 * 1024


def ar5iv_url(paper_id: str) -> str:
    aid = arxiv_path_id(paper_id)
    if not aid:
        raise ProviderError("not_arxiv_id", f"ar5iv requires an arXiv ID: {paper_id}")
    return f"https://{AR5IV_HOST}/html/{aid}"


class Ar5ivProvider:
    """Fetch ar5iv HTML into the content-addressed source repository."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        cache_root: str | Path | None = None,
        source_repository: SourceRepository | None = None,
        request_cache: RemoteRequestCache | None = None,
    ):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.timeout = timeout
        self.cache = request_cache or RemoteRequestCache(
            cache_root, source_repository=source_repository
        )

    def fetch(self, paper_id: str, *, refresh: bool = False) -> SourceArtifact:
        url = ar5iv_url(paper_id)
        aid = arxiv_path_id(paper_id)
        origin = SourceOrigin(
            kind=SourceOriginKind.REMOTE_PROVIDER,
            provider="ar5iv",
            locator=url,
            metadata={"arxiv_id": aid},
        )
        return self.cache.fetch_source(
            "ar5iv-html",
            aid,
            source_format=SourceFormat.HTML,
            media_type=AR5IV_MEDIA_TYPE,
            origin=origin,
            refresh=refresh,
            fetch=lambda: self._fetch_html_bytes(url, paper_id),
        )

    def _fetch_html_bytes(self, url: str, paper_id: str) -> bytes:
        _require_https_host(url, AR5IV_HOST)
        response = self.client.get(url, timeout=self.timeout)
        if response.status_code == 404:
            raise ProviderError(
                "ar5iv_not_found", f"ar5iv HTML not found for {paper_id}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "ar5iv_fetch_failed",
                str(exc),
                status_code=exc.response.status_code,
            ) from exc
        _require_https_host(str(response.url), AR5IV_HOST)
        _validate_response_size(response, MAX_HTML_BYTES, "ar5iv_html_too_large")
        media_type = _response_media_type(response)
        if media_type != AR5IV_MEDIA_TYPE:
            raise ProviderError(
                "ar5iv_media_type_invalid",
                f"ar5iv returned unsupported media type: {media_type or '<missing>'}",
            )
        return bytes(response.content)


def _require_https_host(url: str, host: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ProviderError(
            "remote_url_invalid", f"remote source must use HTTPS on {host}"
        )


def _response_media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()


def _validate_response_size(
    response: httpx.Response, maximum: int, code: str
) -> None:
    content_length = response.headers.get("content-length")
    try:
        if content_length is not None and int(content_length) > maximum:
            raise ProviderError(code, f"remote response exceeds {maximum} bytes")
    except ValueError as exc:
        raise ProviderError(
            "remote_content_length_invalid",
            "remote response has an invalid Content-Length",
        ) from exc
    if len(response.content) > maximum:
        raise ProviderError(code, f"remote response exceeds {maximum} bytes")


__all__ = ["AR5IV_MEDIA_TYPE", "Ar5ivProvider", "MAX_HTML_BYTES", "ar5iv_url"]
