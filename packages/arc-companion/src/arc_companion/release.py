"""Immutable PDF/Web release publication from an accepted book."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import canonical_json_bytes

from .contracts import AcceptedBook
from .project import CompanionProjectPaths
from .renderer import (
    PDF_RENDER_RECIPE,
    WEB_RENDER_RECIPE,
    CompanionRenderer,
)


RELEASE_MANIFEST_SCHEMA = "arc.companion.release_manifest.v1"
DELIVERY_RECIPE = "arc.companion.delivery.v1"
RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v1"


class CompanionReleaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompanionRelease:
    release_id: str
    directory: Path
    pdf: Path
    web_index: Path
    manifest: Path
    reused: bool


class CompanionReleasePublisher:
    def __init__(
        self,
        project: CompanionProjectPaths,
        renderer: CompanionRenderer,
    ) -> None:
        self.project = project
        self.renderer = renderer

    def publish(self, book: AcceptedBook, *, run_id: str) -> CompanionRelease:
        release_id = release_id_for(book)
        target = self.project.releases_root / release_id
        expected_identity = _release_identity(book)
        if target.exists():
            release = self._verify_existing(
                target, release_id=release_id, identity=expected_identity
            )
            self.project.publish_current(
                release_id=release_id,
                manifest=release.manifest,
                run_id=run_id,
            )
            return release
        self.project.releases_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{release_id}.",
                dir=self.project.releases_root,
            )
        )
        try:
            pdf = staging / "companion.pdf"
            web_dir = staging / "reader"
            self.renderer.render_all(book, web_dir=web_dir, pdf_path=pdf)
            files = _file_records(staging)
            document = {
                "schema_version": RELEASE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "identity": expected_identity,
                "files": files,
            }
            manifest = staging / "manifest.json"
            manifest.write_bytes(canonical_json_bytes(document) + b"\n")
            _fsync_tree(staging)
            try:
                os.rename(staging, target)
            except FileExistsError:
                # A cooperating publisher won the exact immutable release.
                shutil.rmtree(staging)
            else:
                _fsync_directory(self.project.releases_root)
            release = self._verify_existing(
                target, release_id=release_id, identity=expected_identity
            )
            self.project.publish_current(
                release_id=release_id,
                manifest=release.manifest,
                run_id=run_id,
            )
            return CompanionRelease(
                release.release_id,
                release.directory,
                release.pdf,
                release.web_index,
                release.manifest,
                False,
            )
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def validate(self, release_id: str, book: AcceptedBook) -> CompanionRelease:
        target = self.project.releases_root / release_id
        release = self._verify_existing(
            target,
            release_id=release_id,
            identity=_release_identity(book),
        )
        self.renderer.validate_pdf(book, release.pdf)
        self.renderer.validate_web(book, release.web_index)
        return release

    def _verify_existing(
        self,
        target: Path,
        *,
        release_id: str,
        identity: dict[str, str],
    ) -> CompanionRelease:
        manifest = target / "manifest.json"
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CompanionReleaseError(
                "release_invalid", "release manifest is unreadable"
            ) from exc
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "release_id",
            "identity",
            "files",
        }:
            raise CompanionReleaseError(
                "release_invalid", "release manifest has invalid fields"
            )
        if (
            value["schema_version"] != RELEASE_MANIFEST_SCHEMA
            or value["release_id"] != release_id
            or value["identity"] != identity
        ):
            raise CompanionReleaseError(
                "release_conflict", "release identity conflicts with its path"
            )
        records = value["files"]
        if not isinstance(records, list) or not records:
            raise CompanionReleaseError(
                "release_invalid", "release manifest has no files"
            )
        record_paths: list[str] = []
        for record in records:
            _verify_file_record(target, record)
            record_paths.append(record["path"])
        if len(record_paths) != len(set(record_paths)):
            raise CompanionReleaseError(
                "release_invalid", "release manifest contains duplicate files"
            )
        actual_paths = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file() and path != manifest
        }
        if set(record_paths) != actual_paths:
            raise CompanionReleaseError(
                "release_invalid",
                "release file set does not exactly match its manifest",
            )
        pdf = target / "companion.pdf"
        web_index = target / "reader" / "index.html"
        if not pdf.is_file() or not web_index.is_file():
            raise CompanionReleaseError(
                "release_invalid", "release is missing PDF or Web output"
            )
        return CompanionRelease(
            release_id,
            target,
            pdf,
            web_index,
            manifest,
            True,
        )


def release_id_for(book: AcceptedBook) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes(_release_identity(book))
    ).hexdigest()
    return f"release-{digest[:24]}"


def _release_identity(book: AcceptedBook) -> dict[str, str]:
    return {
        "accepted_book_digest": book.content_digest,
        "pdf_render_recipe": PDF_RENDER_RECIPE,
        "web_render_recipe": WEB_RENDER_RECIPE,
        "validator_version": RENDER_VALIDATOR_VERSION,
        "delivery_recipe": DELIVERY_RECIPE,
        "manifest_schema": RELEASE_MANIFEST_SCHEMA,
    }


def _file_records(root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        output.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "media_type": _media_type(path),
            }
        )
    return output


def _verify_file_record(root: Path, value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "path",
        "sha256",
        "size",
        "media_type",
    }:
        raise CompanionReleaseError(
            "release_invalid", "release file record has invalid fields"
        )
    relative = value["path"]
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise CompanionReleaseError(
            "release_invalid", "release file path is invalid"
        )
    path = root / relative
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CompanionReleaseError(
            "release_invalid", f"release file is missing: {relative}"
        ) from exc
    if (
        value["size"] != len(payload)
        or value["sha256"] != hashlib.sha256(payload).hexdigest()
        or value["media_type"] != _media_type(path)
    ):
        raise CompanionReleaseError(
            "release_invalid", f"release file does not match manifest: {relative}"
        )


def _media_type(path: Path) -> str:
    return {
        ".css": "text/css",
        ".html": "text/html",
        ".js": "text/javascript",
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ttf": "font/ttf",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _fsync_tree(root: Path) -> None:
    for path in (item for item in root.rglob("*") if item.is_file()):
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    directories = [item for item in root.rglob("*") if item.is_dir()]
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_directory(path)
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DELIVERY_RECIPE",
    "RELEASE_MANIFEST_SCHEMA",
    "RENDER_VALIDATOR_VERSION",
    "CompanionRelease",
    "CompanionReleaseError",
    "CompanionReleasePublisher",
    "release_id_for",
]
