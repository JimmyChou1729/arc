"""Bounded acquisition of one ordinary HTTP(S) resource."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from ..reference_cache import normalize_reference_url
from ._http import response_media_type, validate_response_size
from .base import ProviderError


MAX_HTTP_RESOURCE_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True)
class AcquiredHttpResource:
    payload: bytes
    media_type: str
    requested_url: str
    resolved_url: str
    filename: str


class HttpResourceProvider:
    """Fetch exactly one URL; callers own authorization and source selection."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        maximum_bytes: int = MAX_HTTP_RESOURCE_BYTES,
    ):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.timeout = timeout
        self.maximum_bytes = maximum_bytes

    def fetch(self, url: str) -> AcquiredHttpResource:
        requested = normalize_reference_url(url)
        response = self.client.get(requested, timeout=self.timeout)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                "http_resource_fetch_failed",
                str(exc),
                status_code=exc.response.status_code,
            ) from exc
        try:
            resolved = normalize_reference_url(str(response.url))
        except ValueError as exc:
            raise ProviderError(
                "remote_url_invalid",
                "HTTP resource redirected outside HTTP(S)",
            ) from exc
        validate_response_size(
            response, self.maximum_bytes, "http_resource_too_large"
        )
        media_type = response_media_type(response) or "application/octet-stream"
        return AcquiredHttpResource(
            payload=bytes(response.content),
            media_type=media_type,
            requested_url=requested,
            resolved_url=resolved,
            filename=_filename(response, resolved),
        )


def _filename(response: httpx.Response, url: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    for part in disposition.split(";")[1:]:
        name, separator, value = part.strip().partition("=")
        if separator and name.casefold() == "filename":
            return value.strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1]
    path = unquote(urlparse(url).path)
    return path.rstrip("/").rsplit("/", 1)[-1]


__all__ = [
    "AcquiredHttpResource",
    "HttpResourceProvider",
    "MAX_HTTP_RESOURCE_BYTES",
]
