from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .base import ProviderError


def require_https_host(url: str, host: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != host:
        raise ProviderError(
            "remote_url_invalid", f"remote source must use HTTPS on {host}"
        )


def response_media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()


def validate_response_size(
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


__all__ = ["require_https_host", "response_media_type", "validate_response_size"]
