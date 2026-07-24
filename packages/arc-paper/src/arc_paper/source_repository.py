from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ._file_lock import exclusive_file_lock
from .sources import SourceArtifact, SourceFormat, SourceOrigin, SourceOriginKind


SOURCE_REPOSITORY_SCHEMA = "arc.paper.source_repository.v1"
DEFAULT_MEDIA_TYPES = {
    SourceFormat.HTML: "text/html",
    SourceFormat.MARKDOWN: "text/markdown",
    SourceFormat.TEX: "text/x-tex",
    SourceFormat.PDF: "application/pdf",
}
SOURCE_SUFFIXES = {
    ".html": SourceFormat.HTML,
    ".htm": SourceFormat.HTML,
    ".md": SourceFormat.MARKDOWN,
    ".markdown": SourceFormat.MARKDOWN,
    ".tex": SourceFormat.TEX,
    ".pdf": SourceFormat.PDF,
}
_MANIFEST_FIELDS = {
    "schema_version",
    "source_format",
    "media_type",
    "artifact_digest",
    "size",
}


class SourceRepositoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SourceRepository:
    """Content-addressed storage for immutable paper source bytes.

    The repository owns source bytes and integrity metadata only. It deliberately
    does not own workflow state, retries, queues, or run recovery.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def import_path(
        self,
        path: str | Path,
        *,
        source_format: SourceFormat | str | None = None,
    ) -> SourceArtifact:
        source_path = Path(path)
        resolved_format = (
            SourceFormat(source_format)
            if source_format is not None
            else self._format_for_path(source_path)
        )
        try:
            payload = source_path.read_bytes()
        except OSError as exc:
            raise SourceRepositoryError(
                "source_read_failed", f"unable to read source: {source_path}"
            ) from exc
        return self.store_bytes(
            payload,
            source_format=resolved_format,
            origin=SourceOrigin(
                kind=SourceOriginKind.LOCAL_IMPORT,
                locator=str(source_path),
            ),
        )

    def store_bytes(
        self,
        payload: bytes,
        *,
        source_format: SourceFormat | str,
        origin: SourceOrigin,
        media_type: str | None = None,
    ) -> SourceArtifact:
        if not isinstance(payload, bytes):
            raise TypeError("source payload must be bytes")
        resolved_format = SourceFormat(source_format)
        resolved_media_type = self._normalize_media_type(
            media_type or DEFAULT_MEDIA_TYPES[resolved_format]
        )
        digest = hashlib.sha256(payload).hexdigest()
        object_dir = self._object_dir(resolved_format, digest)
        payload_path = object_dir / "source"
        manifest_path = object_dir / "manifest.json"

        with self._content_lock(resolved_format, digest):
            if manifest_path.exists():
                artifact = self._read_verified(
                    resolved_format, digest, origin=origin
                )
                if (
                    artifact.size != len(payload)
                    or artifact.media_type != resolved_media_type
                ):
                    raise SourceRepositoryError(
                        "source_metadata_conflict",
                        "stored source metadata conflicts with the requested source",
                    )
                return artifact

            object_dir.mkdir(parents=True, exist_ok=True)
            if not self._payload_matches(payload_path, digest, len(payload)):
                self._atomic_write(payload_path, payload)
            manifest = {
                "schema_version": SOURCE_REPOSITORY_SCHEMA,
                "source_format": resolved_format.value,
                "media_type": resolved_media_type,
                "artifact_digest": digest,
                "size": len(payload),
            }
            self._atomic_write(
                manifest_path,
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            self._fsync_dir(object_dir)
            return self._read_verified(resolved_format, digest, origin=origin)

    def get(
        self,
        source_format: SourceFormat | str,
        artifact_digest: str,
    ) -> SourceArtifact:
        resolved_format = SourceFormat(source_format)
        digest = artifact_digest.casefold()
        return self._read_verified(
            resolved_format,
            digest,
            origin=SourceOrigin(
                kind=SourceOriginKind.REPOSITORY,
                locator=f"{resolved_format.value}/sha256/{digest}",
            ),
        )

    def read_bytes(self, artifact: SourceArtifact) -> bytes:
        verified = self._read_verified(
            artifact.source_format,
            artifact.artifact_digest,
            origin=artifact.origin,
        )
        if verified.content_identity != artifact.content_identity:
            raise SourceRepositoryError(
                "source_artifact_mismatch",
                "source artifact metadata does not match repository content",
            )
        return (self._object_dir(
            artifact.source_format, artifact.artifact_digest
        ) / "source").read_bytes()

    def _read_verified(
        self,
        source_format: SourceFormat,
        digest: str,
        *,
        origin: SourceOrigin,
    ) -> SourceArtifact:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SourceRepositoryError(
                "invalid_artifact_digest", "artifact digest must be a SHA-256 digest"
            )
        object_dir = self._object_dir(source_format, digest)
        manifest_path = object_dir / "manifest.json"
        payload_path = object_dir / "source"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise SourceRepositoryError(
                "source_not_found", f"source is not present: {source_format.value}/{digest}"
            ) from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceRepositoryError(
                "source_manifest_invalid", "source manifest is unreadable or malformed"
            ) from exc
        if not isinstance(value, dict) or set(value) != _MANIFEST_FIELDS:
            raise SourceRepositoryError(
                "source_manifest_invalid", "source manifest has an invalid schema"
            )
        expected = {
            "schema_version": SOURCE_REPOSITORY_SCHEMA,
            "source_format": source_format.value,
            "artifact_digest": digest,
        }
        if any(value.get(key) != item for key, item in expected.items()):
            raise SourceRepositoryError(
                "source_manifest_invalid", "source manifest identity does not match its key"
            )
        media_type = value.get("media_type")
        size = value.get("size")
        if (
            not isinstance(media_type, str)
            or not media_type
            or ";" in media_type
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise SourceRepositoryError(
                "source_manifest_invalid", "source manifest metadata is invalid"
            )
        if not self._payload_matches(payload_path, digest, size):
            raise SourceRepositoryError(
                "source_corrupt", "source bytes do not match the manifest"
            )
        return SourceArtifact(
            source_format=source_format,
            artifact_digest=digest,
            size=size,
            media_type=media_type,
            origin=origin,
        )

    def _object_dir(self, source_format: SourceFormat, digest: str) -> Path:
        return (
            self.root
            / "source-repository"
            / "v1"
            / source_format.value
            / "sha256"
            / digest[:2]
            / digest
        )

    @contextmanager
    def _content_lock(
        self, source_format: SourceFormat, digest: str
    ) -> Iterator[None]:
        lock_path = (
            self.root
            / "source-repository"
            / "v1"
            / "locks"
            / source_format.value
            / f"{digest}.lock"
        )
        with exclusive_file_lock(lock_path):
            yield

    @staticmethod
    def _payload_matches(path: Path, digest: str, size: int) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != size:
                return False
            hasher = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return hasher.hexdigest() == digest
        except OSError:
            return False

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            SourceRepository._fsync_dir(path.parent)
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        if os.name == "nt":
            # Windows does not expose a portable directory fsync operation.
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _normalize_media_type(media_type: str) -> str:
        normalized = media_type.strip().casefold()
        if not normalized or ";" in normalized or "/" not in normalized:
            raise ValueError("media_type must be a normalized MIME type")
        return normalized

    @staticmethod
    def _format_for_path(path: Path) -> SourceFormat:
        try:
            return SOURCE_SUFFIXES[path.suffix.casefold()]
        except KeyError as exc:
            raise SourceRepositoryError(
                "unsupported_source",
                f"unsupported local source suffix: {path.suffix or '<none>'}",
            ) from exc


__all__ = [
    "DEFAULT_MEDIA_TYPES",
    "SOURCE_REPOSITORY_SCHEMA",
    "SourceRepository",
    "SourceRepositoryError",
]
