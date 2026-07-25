from __future__ import annotations

from pathlib import Path

import httpx

from ..ids import arxiv_path_id
from ..source_repository import SourceRepository
from ..sources import SourceArtifact, SourceFormat, SourceOrigin, SourceOriginKind
from ._http import require_https_host, response_media_type, validate_response_size
from ._request_gate import HostRequestGate, shared_host_gate
from .base import ProviderError
from .remote_cache import RemoteCacheError, RemoteRequestCache


ARXIV_HOST = "arxiv.org"
ARXIV_HTML_MEDIA_TYPE = "text/html"
ARXIV_HTML_AVAILABILITY_NAMESPACE = "arxiv-html-availability"
MAX_HTML_BYTES = 50 * 1024 * 1024


def arxiv_html_url(paper_id: str) -> str:
    aid = arxiv_path_id(paper_id)
    if not aid:
        raise ProviderError(
            "not_arxiv_id", f"arXiv HTML requires an arXiv ID: {paper_id}"
        )
    return f"https://{ARXIV_HOST}/html/{aid}"


class ArxivHtmlProvider:
    """Fetch official arXiv HTML, caching deterministic absence separately."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        cache_root: str | Path | None = None,
        source_repository: SourceRepository | None = None,
        request_cache: RemoteRequestCache | None = None,
        request_gate: HostRequestGate | None = None,
    ) -> None:
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
        url = arxiv_html_url(paper_id)
        origin = SourceOrigin(
            kind=SourceOriginKind.REMOTE_PROVIDER,
            provider="arxiv-html",
            locator=url,
            metadata={"arxiv_id": aid},
        )
        known_not_found = self._is_known_not_found(aid)
        if not refresh and known_not_found:
            raise _not_found_error(paper_id)
        if known_not_found:
            # Refresh must not leave a stale 404 marker after a transient error.
            self.cache.remove("json", ARXIV_HTML_AVAILABILITY_NAMESPACE, aid)
        artifact = self.cache.fetch_source(
            "arxiv-html",
            aid,
            source_format=SourceFormat.HTML,
            media_type=ARXIV_HTML_MEDIA_TYPE,
            origin=origin,
            refresh=refresh,
            fetch=lambda: self._fetch_html_bytes(
                url,
                paper_id,
                aid,
                honor_cached_not_found=not refresh,
            ),
        )
        return artifact

    def _is_known_not_found(self, aid: str) -> bool:
        try:
            value = self.cache.get_json(ARXIV_HTML_AVAILABILITY_NAMESPACE, aid)
        except RemoteCacheError as exc:
            if exc.code != "remote_cache_json_corrupt":
                raise
            self.cache.remove("json", ARXIV_HTML_AVAILABILITY_NAMESPACE, aid)
            return False
        return value == {"status": "not_found"}

    def _fetch_html_bytes(
        self,
        url: str,
        paper_id: str,
        aid: str,
        *,
        honor_cached_not_found: bool,
    ) -> bytes:
        if honor_cached_not_found and self._is_known_not_found(aid):
            raise _not_found_error(paper_id)
        require_https_host(url, ARXIV_HOST)
        response = self.request_gate.request(
            lambda: self.client.get(url, timeout=self.timeout)
        )
        if response.status_code == 404:
            self.cache.fetch_json(
                ARXIV_HTML_AVAILABILITY_NAMESPACE,
                aid,
                fetch=lambda: {"status": "not_found"},
                refresh=True,
            )
            raise _not_found_error(paper_id)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "arxiv_html_fetch_failed",
                str(exc),
                status_code=exc.response.status_code,
            ) from exc
        require_https_host(str(response.url), ARXIV_HOST)
        validate_response_size(response, MAX_HTML_BYTES, "arxiv_html_too_large")
        media_type = response_media_type(response)
        if media_type != ARXIV_HTML_MEDIA_TYPE:
            raise ProviderError(
                "arxiv_html_media_type_invalid",
                "arXiv returned unsupported media type: "
                f"{media_type or '<missing>'}",
            )
        return bytes(response.content)


def _not_found_error(paper_id: str) -> ProviderError:
    return ProviderError(
        "arxiv_html_not_found", f"arXiv HTML not found for {paper_id}"
    )


__all__ = [
    "ARXIV_HTML_AVAILABILITY_NAMESPACE",
    "ARXIV_HTML_MEDIA_TYPE",
    "ArxivHtmlProvider",
    "MAX_HTML_BYTES",
    "arxiv_html_url",
]
