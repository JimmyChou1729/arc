from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .._cache_root import resolve_cache_root
from .._durable_io import atomic_write_bytes, payload_matches
from .._file_lock import exclusive_file_lock
from ..source_repository import SourceRepository, SourceRepositoryError
from ..sources import SourceArtifact, SourceFormat, SourceOrigin


REMOTE_SOURCE_CACHE_SCHEMA = "arc.paper.remote_source_cache.v1"
REMOTE_JSON_CACHE_SCHEMA = "arc.paper.remote_json_cache.v1"
_SOURCE_FIELDS = {
    "schema_version",
    "namespace",
    "request_digest",
    "source_format",
    "media_type",
    "artifact_digest",
    "size",
}
_JSON_FIELDS = {
    "schema_version",
    "namespace",
    "request_digest",
    "payload_file",
    "artifact_digest",
    "size",
}


class RemoteCacheError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def default_cache_root() -> Path:
    return resolve_cache_root()


class RemoteRequestCache:
    """Request-key mappings for remote reads.

    Source bytes are stored by ``SourceRepository``. This class owns only the
    mapping from a stable provider request to those immutable bytes, plus the
    small JSON cache needed for metadata APIs. It has no run or retry state.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        source_repository: SourceRepository | None = None,
    ):
        self.root = resolve_cache_root(root, repository=source_repository)
        self.source_repository = source_repository or SourceRepository(self.root)

    def get_source(
        self,
        namespace: str,
        request_key: str,
        *,
        source_format: SourceFormat | str,
        media_type: str,
        origin: SourceOrigin,
    ) -> SourceArtifact | None:
        resolved_format = SourceFormat(source_format)
        normalized_media = _normalize_media_type(media_type)
        digest = _request_digest(namespace, request_key)
        manifest_path = self._entry_dir("source", namespace, digest) / "manifest.json"
        if not manifest_path.exists():
            return None
        value = self._read_manifest(manifest_path)
        if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote source cache manifest has an invalid schema",
            )
        expected = {
            "schema_version": REMOTE_SOURCE_CACHE_SCHEMA,
            "namespace": namespace,
            "request_digest": digest,
            "source_format": resolved_format.value,
            "media_type": normalized_media,
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote source cache manifest does not match its request key",
            )
        artifact_digest = value.get("artifact_digest")
        size = value.get("size")
        if (
            not isinstance(artifact_digest, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote source cache manifest contains invalid artifact metadata",
            )
        try:
            artifact = self.source_repository.get(resolved_format, artifact_digest)
            self.source_repository.read_bytes(artifact)
        except SourceRepositoryError as exc:
            raise RemoteCacheError(
                "remote_cache_source_corrupt",
                "remote source cache points to missing or corrupt source bytes",
            ) from exc
        if artifact.size != size or artifact.media_type != normalized_media:
            raise RemoteCacheError(
                "remote_cache_source_corrupt",
                "remote source cache metadata does not match source bytes",
            )
        return SourceArtifact(
            source_format=artifact.source_format,
            artifact_digest=artifact.artifact_digest,
            size=artifact.size,
            media_type=artifact.media_type,
            origin=origin,
        )

    def fetch_source(
        self,
        namespace: str,
        request_key: str,
        *,
        source_format: SourceFormat | str,
        media_type: str,
        origin: SourceOrigin,
        fetch: Callable[[], bytes],
        refresh: bool = False,
    ) -> SourceArtifact:
        if not refresh:
            cached = self.get_source(
                namespace,
                request_key,
                source_format=source_format,
                media_type=media_type,
                origin=origin,
            )
            if cached is not None:
                return cached
        digest = _request_digest(namespace, request_key)
        with self._request_lock("source", namespace, digest):
            if not refresh:
                cached = self.get_source(
                    namespace,
                    request_key,
                    source_format=source_format,
                    media_type=media_type,
                    origin=origin,
                )
                if cached is not None:
                    return cached
            payload = fetch()
            if not isinstance(payload, bytes):
                raise TypeError("remote source fetch must return bytes")
            artifact = self.source_repository.store_bytes(
                payload,
                source_format=source_format,
                media_type=media_type,
                origin=origin,
            )
            entry_dir = self._entry_dir("source", namespace, digest)
            manifest = {
                "schema_version": REMOTE_SOURCE_CACHE_SCHEMA,
                "namespace": namespace,
                "request_digest": digest,
                "source_format": artifact.source_format.value,
                "media_type": artifact.media_type,
                "artifact_digest": artifact.artifact_digest,
                "size": artifact.size,
            }
            self._atomic_write(
                entry_dir / "manifest.json", _canonical_json_bytes(manifest)
            )
            return artifact

    def get_json(self, namespace: str, request_key: str) -> Any | None:
        digest = _request_digest(namespace, request_key)
        entry_dir = self._entry_dir("json", namespace, digest)
        manifest_path = entry_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        value = self._read_manifest(manifest_path)
        if not isinstance(value, dict) or set(value) != _JSON_FIELDS:
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote JSON cache manifest has an invalid schema",
            )
        expected = {
            "schema_version": REMOTE_JSON_CACHE_SCHEMA,
            "namespace": namespace,
            "request_digest": digest,
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote JSON cache manifest does not match its request key",
            )
        artifact_digest = value.get("artifact_digest")
        size = value.get("size")
        payload_file = value.get("payload_file")
        if (
            not isinstance(artifact_digest, str)
            or len(artifact_digest) != 64
            or any(char not in "0123456789abcdef" for char in artifact_digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(payload_file, str)
            or payload_file != _json_payload_file(artifact_digest)
        ):
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote JSON cache manifest contains invalid artifact metadata",
            )
        payload_path = entry_dir / payload_file
        if not _payload_matches(payload_path, artifact_digest, size):
            raise RemoteCacheError(
                "remote_cache_json_corrupt",
                "remote JSON cache payload does not match its manifest",
            )
        try:
            return json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RemoteCacheError(
                "remote_cache_json_corrupt",
                "remote JSON cache payload is unreadable or malformed",
            ) from exc

    def fetch_json(
        self,
        namespace: str,
        request_key: str,
        *,
        fetch: Callable[[], Any],
        refresh: bool = False,
    ) -> Any:
        if not refresh:
            cached = self.get_json(namespace, request_key)
            if cached is not None:
                return cached
        digest = _request_digest(namespace, request_key)
        with self._request_lock("json", namespace, digest):
            if not refresh:
                cached = self.get_json(namespace, request_key)
                if cached is not None:
                    return cached
            value = fetch()
            payload = _canonical_json_bytes(value)
            artifact_digest = hashlib.sha256(payload).hexdigest()
            entry_dir = self._entry_dir("json", namespace, digest)
            payload_file = _json_payload_file(artifact_digest)
            self._atomic_write(entry_dir / payload_file, payload)
            manifest = {
                "schema_version": REMOTE_JSON_CACHE_SCHEMA,
                "namespace": namespace,
                "request_digest": digest,
                "payload_file": payload_file,
                "artifact_digest": artifact_digest,
                "size": len(payload),
            }
            self._atomic_write(
                entry_dir / "manifest.json", _canonical_json_bytes(manifest)
            )
            return value

    def _entry_dir(self, kind: str, namespace: str, digest: str) -> Path:
        safe_namespace = _safe_namespace(namespace)
        return (
            self.root
            / "remote-request-cache"
            / "v1"
            / kind
            / safe_namespace
            / digest[:2]
            / digest
        )

    @contextmanager
    def _request_lock(
        self, kind: str, namespace: str, digest: str
    ) -> Iterator[None]:
        path = (
            self.root
            / "remote-request-cache"
            / "v1"
            / "locks"
            / kind
            / _safe_namespace(namespace)
            / f"{digest}.lock"
        )
        with exclusive_file_lock(path):
            yield

    @staticmethod
    def _read_manifest(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RemoteCacheError(
                "remote_cache_manifest_invalid",
                "remote cache manifest is unreadable or malformed",
            ) from exc

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        atomic_write_bytes(path, payload)


def _request_digest(namespace: str, request_key: str) -> str:
    if not namespace or not request_key:
        raise ValueError("remote cache namespace and request key are required")
    return hashlib.sha256(
        _canonical_json_bytes({"namespace": namespace, "request_key": request_key})
    ).hexdigest()


def _safe_namespace(namespace: str) -> str:
    normalized = "".join(
        char if char.isalnum() or char in "._-" else "_" for char in namespace
    ).strip("._")
    if not normalized:
        raise ValueError("remote cache namespace must contain a safe character")
    return normalized


def _json_payload_file(artifact_digest: str) -> str:
    return f"payloads/{artifact_digest}.json"


def _normalize_media_type(media_type: str) -> str:
    normalized = media_type.strip().casefold()
    if not normalized or "/" not in normalized or ";" in normalized:
        raise ValueError("media_type must be a normalized MIME type")
    return normalized


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RemoteCacheError(
            "remote_cache_json_invalid", "remote JSON response is not serializable"
        ) from exc


def _payload_matches(path: Path, digest: str, size: int) -> bool:
    return payload_matches(path, digest, size)


__all__ = [
    "REMOTE_JSON_CACHE_SCHEMA",
    "REMOTE_SOURCE_CACHE_SCHEMA",
    "RemoteCacheError",
    "RemoteRequestCache",
    "default_cache_root",
]
