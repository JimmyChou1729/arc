from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .._cache_root import resolve_cache_root
from .._durable_io import atomic_write_bytes, payload_matches
from .._file_lock import exclusive_file_lock
from ..source_repository import SourceRepository, SourceRepositoryError
from ..sources import SourceArtifact, SourceFormat, SourceOrigin


REMOTE_SOURCE_CACHE_SCHEMA = "arc.paper.remote_source_cache.v2"
REMOTE_JSON_CACHE_SCHEMA = "arc.paper.remote_json_cache.v2"
REMOTE_CACHE_ADMIN_SCHEMA = "arc.paper.remote_cache_admin.v2"
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
_ADMIN_FIELDS = {
    "schema_version",
    "kind",
    "namespace",
    "request_key",
    "request_digest",
    "cached_at",
}


class RemoteCacheError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RemoteCacheAdminEntry:
    """One physical remote-request mapping visible to cache administration."""

    entry_id: str
    kind: str
    namespace: str
    request_digest: str
    request_key: str
    cached_at: str


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
        self._require_admin(
            manifest_path.parent,
            kind="source",
            namespace=namespace,
            request_key=request_key,
            digest=digest,
            corruption_code="remote_cache_source_corrupt",
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
        digest = _request_digest(namespace, request_key)
        with self._request_lock("source", namespace, digest):
            if not refresh:
                try:
                    cached = self.get_source(
                        namespace,
                        request_key,
                        source_format=source_format,
                        media_type=media_type,
                        origin=origin,
                    )
                except RemoteCacheError as exc:
                    if exc.code != "remote_cache_source_corrupt":
                        raise
                    self._remove_entry_dir(
                        "source",
                        self._entry_dir("source", namespace, digest),
                    )
                    cached = None
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
            self._write_admin(
                entry_dir,
                kind="source",
                namespace=namespace,
                request_key=request_key,
                request_digest=digest,
            )
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
        self._require_admin(
            entry_dir,
            kind="json",
            namespace=namespace,
            request_key=request_key,
            digest=digest,
            corruption_code="remote_cache_json_corrupt",
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
        payload_validator: Callable[[Any], bool] | None = None,
    ) -> Any:
        digest = _request_digest(namespace, request_key)
        with self._request_lock("json", namespace, digest):
            if not refresh:
                try:
                    cached = self.get_json(namespace, request_key)
                except RemoteCacheError as exc:
                    if exc.code != "remote_cache_json_corrupt":
                        raise
                    self._remove_entry_dir(
                        "json",
                        self._entry_dir("json", namespace, digest),
                    )
                    cached = None
                if cached is not None:
                    if _payload_is_valid(cached, payload_validator):
                        return cached
                    self._remove_entry_dir(
                        "json",
                        self._entry_dir("json", namespace, digest),
                    )
            value = fetch()
            if not _payload_is_valid(value, payload_validator):
                raise RemoteCacheError(
                    "remote_cache_payload_contract_invalid",
                    "remote JSON response does not satisfy its payload contract",
                )
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
            self._write_admin(
                entry_dir,
                kind="json",
                namespace=namespace,
                request_key=request_key,
                request_digest=digest,
            )
            self._atomic_write(
                entry_dir / "manifest.json", _canonical_json_bytes(manifest)
            )
            return value

    def admin_entry(
        self,
        kind: str,
        namespace: str,
        request_key: str,
    ) -> RemoteCacheAdminEntry | None:
        """Return cache-write metadata without touching its timestamp."""

        digest = _request_digest(namespace, request_key)
        entry_dir = self._entry_dir(kind, namespace, digest)
        if not (entry_dir / "manifest.json").is_file():
            return None
        return self._admin_entry_from_dir(
            entry_dir,
            kind=kind,
            path_namespace=_safe_namespace(namespace),
            digest=digest,
        )

    def admin_entries(self) -> tuple[RemoteCacheAdminEntry, ...]:
        """Enumerate strict current request mappings."""

        root = self.root / "remote-request-cache" / "v2"
        entries: list[RemoteCacheAdminEntry] = []
        for kind in ("json", "source"):
            kind_root = root / kind
            if not kind_root.is_dir():
                continue
            for manifest_path in kind_root.glob("*/*/*/manifest.json"):
                entry_dir = manifest_path.parent
                digest = entry_dir.name
                if len(digest) != 64:
                    continue
                entry = self._admin_entry_from_dir(
                    entry_dir,
                    kind=kind,
                    path_namespace=entry_dir.parents[1].name,
                    digest=digest,
                )
                if entry is not None:
                    entries.append(entry)
        return tuple(sorted(entries, key=lambda item: item.entry_id))

    def remove(self, kind: str, namespace: str, request_key: str) -> bool:
        """Physically remove one request mapping and its exact source object."""

        digest = _request_digest(namespace, request_key)
        return self.remove_admin_entry(
            _remote_entry_id(kind, _safe_namespace(namespace), digest)
        )

    def remove_admin_entry(self, entry_id: str) -> bool:
        """Remove one exact current mapping."""

        selected = next(
            (item for item in self.admin_entries() if item.entry_id == entry_id),
            None,
        )
        if selected is None:
            return False
        entry_dir = (
            self.root
            / "remote-request-cache"
            / "v2"
            / selected.kind
            / _safe_namespace(selected.namespace)
            / selected.request_digest[:2]
            / selected.request_digest
        )
        with self._request_lock(
            selected.kind, selected.namespace, selected.request_digest
        ):
            return self._remove_entry_dir(selected.kind, entry_dir)

    def _remove_entry_dir(self, kind: str, entry_dir: Path) -> bool:
        """Delete one mapping while its request lock is held by the caller."""

        if not entry_dir.exists():
            return False
        source_identity: tuple[str, str] | None = None
        if kind == "source":
            try:
                value = self._read_manifest(entry_dir / "manifest.json")
                source_format = value.get("source_format")
                artifact_digest = value.get("artifact_digest")
                if (
                    isinstance(source_format, str)
                    and isinstance(artifact_digest, str)
                ):
                    source_identity = (source_format, artifact_digest)
            except (AttributeError, RemoteCacheError):
                pass
        shutil.rmtree(entry_dir)
        if source_identity is not None:
            self.source_repository.remove(*source_identity)
        return True

    def _write_admin(
        self,
        entry_dir: Path,
        *,
        kind: str,
        namespace: str,
        request_key: str,
        request_digest: str,
    ) -> None:
        value = {
            "schema_version": REMOTE_CACHE_ADMIN_SCHEMA,
            "kind": kind,
            "namespace": namespace,
            "request_key": request_key,
            "request_digest": request_digest,
            "cached_at": _utc_now(),
        }
        self._atomic_write(entry_dir / "admin.json", _canonical_json_bytes(value))

    def _admin_entry_from_dir(
        self,
        entry_dir: Path,
        *,
        kind: str,
        path_namespace: str,
        digest: str,
    ) -> RemoteCacheAdminEntry | None:
        try:
            value = json.loads((entry_dir / "admin.json").read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            namespace = value.get("namespace")
            request_key = value.get("request_key")
            if (
                set(value) == _ADMIN_FIELDS
                and value.get("schema_version") == REMOTE_CACHE_ADMIN_SCHEMA
                and value.get("kind") == kind
                and isinstance(namespace, str)
                and _safe_namespace(namespace) == path_namespace
                and value.get("request_digest") == digest
                and isinstance(request_key, str)
                and request_key
                and _request_digest(namespace, request_key) == digest
                and isinstance(value.get("cached_at"), str)
                and value["cached_at"]
                and _is_utc_timestamp(value["cached_at"])
                and self._manifest_contract_is_current(
                    entry_dir / "manifest.json",
                    kind=kind,
                    namespace=namespace,
                    digest=digest,
                )
            ):
                return RemoteCacheAdminEntry(
                    entry_id=_remote_entry_id(kind, path_namespace, digest),
                    kind=kind,
                    namespace=namespace,
                    request_digest=digest,
                    request_key=request_key,
                    cached_at=value["cached_at"],
                )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            pass
        return None

    def _require_admin(
        self,
        entry_dir: Path,
        *,
        kind: str,
        namespace: str,
        request_key: str,
        digest: str,
        corruption_code: str,
    ) -> None:
        entry = self._admin_entry_from_dir(
            entry_dir,
            kind=kind,
            path_namespace=_safe_namespace(namespace),
            digest=digest,
        )
        if entry is None or entry.request_key != request_key:
            raise RemoteCacheError(
                corruption_code,
                "remote cache mapping has no valid current admin metadata",
            )

    def _manifest_contract_is_current(
        self,
        path: Path,
        *,
        kind: str,
        namespace: str,
        digest: str,
    ) -> bool:
        try:
            value = self._read_manifest(path)
        except RemoteCacheError:
            return False
        if kind == "source":
            fields = _SOURCE_FIELDS
            schema = REMOTE_SOURCE_CACHE_SCHEMA
        elif kind == "json":
            fields = _JSON_FIELDS
            schema = REMOTE_JSON_CACHE_SCHEMA
        else:
            return False
        return (
            isinstance(value, dict)
            and set(value) == fields
            and value.get("schema_version") == schema
            and value.get("namespace") == namespace
            and value.get("request_digest") == digest
        )

    def _entry_dir(self, kind: str, namespace: str, digest: str) -> Path:
        safe_namespace = _safe_namespace(namespace)
        return (
            self.root
            / "remote-request-cache"
            / "v2"
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
            / "v2"
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


def _remote_entry_id(kind: str, namespace: str, digest: str) -> str:
    return f"remote:{kind}:{namespace}:{digest}"


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


def _payload_is_valid(
    value: Any,
    validator: Callable[[Any], bool] | None,
) -> bool:
    if validator is None:
        return True
    try:
        return validator(value) is True
    except Exception:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_utc_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


__all__ = [
    "REMOTE_JSON_CACHE_SCHEMA",
    "REMOTE_SOURCE_CACHE_SCHEMA",
    "REMOTE_CACHE_ADMIN_SCHEMA",
    "RemoteCacheAdminEntry",
    "RemoteCacheError",
    "RemoteRequestCache",
    "default_cache_root",
]
