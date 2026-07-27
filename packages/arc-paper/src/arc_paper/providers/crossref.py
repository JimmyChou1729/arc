"""Generic DOI metadata through Crossref's public works API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from ..ids import doi_value
from ._http import response_media_type, validate_response_size
from .base import ProviderError
from .remote_cache import RemoteRequestCache


CROSSREF_API_HOST = "api.crossref.org"
CROSSREF_API_URL = f"https://{CROSSREF_API_HOST}/works"
MAX_CROSSREF_BYTES = 50 * 1024 * 1024


class CrossrefProvider:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        cache_root: str | Path | None = None,
        request_cache: RemoteRequestCache | None = None,
    ):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.timeout = timeout
        self.cache = request_cache or RemoteRequestCache(cache_root)

    def get_metadata(self, doi: str, *, refresh: bool = False) -> dict[str, Any]:
        normalized = doi_value(doi)
        if not normalized:
            raise ProviderError("crossref_doi_invalid", f"Crossref requires a DOI: {doi}")
        value = self.cache.fetch_json(
            "crossref-work",
            normalized,
            fetch=lambda: self._fetch(normalized),
            refresh=refresh,
        )
        if not isinstance(value, dict):
            raise ProviderError(
                "crossref_response_invalid", "Crossref metadata cache is not an object"
            )
        return _normalize_crossref_message(value)

    def _fetch(self, doi: str) -> dict[str, Any]:
        url = f"{CROSSREF_API_URL}/{quote(doi, safe='')}"
        response = self.client.get(url, timeout=self.timeout)
        if response.status_code == 404:
            raise ProviderError("crossref_not_found", f"Crossref record not found for {doi}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "crossref_fetch_failed",
                str(exc),
                status_code=exc.response.status_code,
            ) from exc
        parsed = urlparse(str(response.url))
        if parsed.scheme != "https" or parsed.hostname != CROSSREF_API_HOST:
            raise ProviderError(
                "remote_url_invalid",
                f"Crossref requests must stay on {CROSSREF_API_HOST}",
            )
        validate_response_size(
            response, MAX_CROSSREF_BYTES, "crossref_response_too_large"
        )
        media_type = response_media_type(response)
        if media_type not in {"application/json", "application/vnd.api+json"}:
            raise ProviderError(
                "crossref_media_type_invalid",
                f"Crossref returned unsupported media type: {media_type or '<missing>'}",
            )
        try:
            value = response.json()
        except (ValueError, UnicodeError) as exc:
            raise ProviderError(
                "crossref_response_invalid", "Crossref returned invalid JSON"
            ) from exc
        message = value.get("message") if isinstance(value, dict) else None
        if not isinstance(message, dict):
            raise ProviderError(
                "crossref_response_invalid", "Crossref response has no work metadata"
            )
        return message


def _normalize_crossref_message(value: dict[str, Any]) -> dict[str, Any]:
    raw_doi = str(value.get("DOI") or "")
    doi = doi_value(raw_doi)
    if not doi:
        raise ProviderError(
            "crossref_response_invalid", "Crossref work metadata has no valid DOI"
        )
    title = _first_string(value.get("title"))
    landing_url = str(value.get("URL") or f"https://doi.org/{doi}").strip()
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value.get("link") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("URL") or "").strip()
        media_type = (
            str(item.get("content-type") or "application/octet-stream")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if url and urlparse(url).scheme.casefold() in {"http", "https"}:
            key = (url, media_type)
            if key not in seen:
                seen.add(key)
                links.append({"url": url, "media_type": media_type})
    authors = []
    for author in value.get("author") or []:
        if not isinstance(author, dict):
            continue
        parts = (
            str(author.get("given") or "").strip(),
            str(author.get("family") or "").strip(),
        )
        name = " ".join(part for part in parts if part)
        if name:
            authors.append(name)
    return {
        "paper_id": f"doi:{doi}",
        "title": title,
        "abstract": str(value.get("abstract") or ""),
        "authors": authors,
        "arxiv_id": "",
        "inspire_recid": "",
        "doi": doi,
        "dois": [doi],
        "identifiers": {"paper_id": f"doi:{doi}", "doi": doi},
        "year": _published_year(value),
        "published": _published_text(value),
        "citation_count": int(value.get("is-referenced-by-count") or 0),
        "landing_url": landing_url,
        "links": links,
    }


def _first_string(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0] if value else "").strip()
    return str(value or "").strip()


def _published_parts(value: dict[str, Any]) -> list[int]:
    for key in ("published", "published-print", "published-online", "issued"):
        publication = value.get(key)
        date_parts = (
            publication.get("date-parts")
            if isinstance(publication, dict)
            else None
        )
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
        ):
            return [
                int(item)
                for item in date_parts[0]
                if isinstance(item, int) and not isinstance(item, bool)
            ]
    return []


def _published_year(value: dict[str, Any]) -> int | None:
    parts = _published_parts(value)
    return parts[0] if parts else None


def _published_text(value: dict[str, Any]) -> str:
    parts = _published_parts(value)
    return "-".join(
        str(item) if index == 0 else f"{item:02d}"
        for index, item in enumerate(parts)
    )


__all__ = [
    "CROSSREF_API_HOST",
    "CROSSREF_API_URL",
    "CrossrefProvider",
    "MAX_CROSSREF_BYTES",
]
