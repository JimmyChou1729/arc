"""Immutable release publication from an accepted book."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_bytes, canonical_json_bytes, file_lease

from .contracts import AcceptedBook
from .project import CompanionProjectPaths
from .renderer import (
    PDF_RENDER_RECIPE,
    WEB_RENDER_RECIPE,
    CompanionRenderer,
)
from .standalone_html import StandaloneHtmlError, standalone_html_bytes


_LEGACY_RELEASE_MANIFEST_SCHEMA = "arc.companion.release_manifest.v1"
RELEASE_MANIFEST_SCHEMA = "arc.companion.release_manifest.v2"
_LEGACY_DELIVERY_RECIPE = "arc.companion.delivery.v1"
_BASE_DELIVERY_RECIPE = "arc.companion.delivery.v2"
_OLDER_DELIVERY_RECIPE = "arc.companion.delivery.v3"
_PREVIOUS_DELIVERY_RECIPE = "arc.companion.delivery.v4"
_FORMER_DELIVERY_RECIPE = "arc.companion.delivery.v5"
DELIVERY_RECIPE = "arc.companion.delivery.v6"
_LEGACY_PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v9"
_LEGACY_WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v7"
_LEGACY_RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v4"
_PREVIOUS_PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v10"
_PREVIOUS_WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v8"
_PREVIOUS_RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v5"
_LAST_PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v11"
_LAST_WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v9"
_LAST_RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v6"
_FORMER_PDF_RENDER_RECIPE = "arc.companion.pdf.source_anchored.v13"
_FORMER_WEB_RENDER_RECIPE = "arc.companion.web.source_anchored.v11"
_FORMER_RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v7"
RENDER_VALIDATOR_VERSION = "arc.companion.render_validator.v8"
_FULL_FORMATS = ("pdf", "web")
_WEB_ONLY_FORMATS = ("web",)
_WINDOWS = os.name == "nt"


class CompanionReleaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CompanionRelease:
    release_id: str
    directory: Path
    available_formats: tuple[str, ...]
    pdf: Path | None
    web_index: Path
    manifest: Path
    reused: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    payload: bytes = b""


@dataclass(frozen=True)
class _DeliverySnapshot:
    pdf: _FileSnapshot
    html: _FileSnapshot
    current: _FileSnapshot


class CompanionReleasePublisher:
    def __init__(
        self,
        project: CompanionProjectPaths,
        renderer: CompanionRenderer,
    ) -> None:
        self.project = project
        self.renderer = renderer

    def publish(self, book: AcceptedBook, *, run_id: str) -> CompanionRelease:
        with file_lease(self.project.delivery_lease, blocking=True):
            self._assert_delivery_targets_known()
            return self._publish_locked(book, run_id=run_id)

    def _publish_locked(
        self,
        book: AcceptedBook,
        *,
        run_id: str,
    ) -> CompanionRelease:
        full_release_id = release_id_for(book)
        full_target = self.project.releases_root / full_release_id
        if full_target.exists():
            release = self._verify_existing(
                full_target,
                release_id=full_release_id,
                book=book,
            )
            if release.available_formats != _FULL_FORMATS:
                raise CompanionReleaseError(
                    "release_conflict",
                    "complete release path does not contain both PDF and Web",
                )
            self._validate_release_artifacts(release, book)
            self._publish_delivery(release, run_id=run_id)
            return release
        self.project.releases_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=".release.",
                dir=self.project.releases_root,
            )
        )
        reused = False
        try:
            pdf = staging / "companion.pdf"
            web_dir = staging / "reader"
            rendered = self.renderer.render_all(
                book, web_dir=web_dir, pdf_path=pdf
            )
            if (
                rendered.accepted_book_digest != book.content_digest
                or rendered.web_index is None
            ):
                raise CompanionReleaseError(
                    "release_invalid",
                    "renderer did not return a Web reader for the accepted book",
                )
            available_formats = (
                _FULL_FORMATS
                if rendered.pdf_path is not None
                else _WEB_ONLY_FORMATS
            )
            expected_identity = _release_identity(
                book, available_formats=available_formats
            )
            release_id = _release_id(expected_identity)
            target = self.project.releases_root / release_id
            files = _file_records(staging)
            document = {
                "schema_version": RELEASE_MANIFEST_SCHEMA,
                "release_id": release_id,
                "identity": expected_identity,
                "available_formats": list(available_formats),
                "files": files,
            }
            manifest = staging / "manifest.json"
            manifest.write_bytes(canonical_json_bytes(document) + b"\n")
            self._verify_existing(
                staging,
                release_id=release_id,
                book=book,
            )
            _fsync_tree(staging)
            try:
                os.rename(staging, target)
            except OSError as exc:
                if (
                    exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}
                    or not target.is_dir()
                ):
                    raise
                # A cooperating publisher won the exact immutable release.
                shutil.rmtree(staging)
                reused = True
            else:
                _fsync_directory(self.project.releases_root)
            release = self._verify_existing(
                target,
                release_id=release_id,
                book=book,
            )
            if reused:
                self._validate_release_artifacts(release, book)
            published = CompanionRelease(
                release.release_id,
                release.directory,
                release.available_formats,
                release.pdf,
                release.web_index,
                release.manifest,
                reused,
                tuple(rendered.warnings),
            )
            self._publish_delivery(published, run_id=run_id)
            return published
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def validate(self, release_id: str, book: AcceptedBook) -> CompanionRelease:
        target = self.project.releases_root / release_id
        release = self._verify_existing(
            target,
            release_id=release_id,
            book=book,
        )
        self._validate_release_artifacts(release, book)
        return release

    def _validate_release_artifacts(
        self, release: CompanionRelease, book: AcceptedBook
    ) -> None:
        self.renderer.validate_web(book, release.web_index)
        if release.pdf is not None:
            self.renderer.validate_pdf(book, release.pdf)

    def validate_current(
        self,
        pointer: dict[str, Any],
        book: AcceptedBook,
    ) -> CompanionRelease:
        with file_lease(self.project.delivery_lease, blocking=True):
            return self._validate_current_locked(pointer, book)

    def _validate_current_locked(
        self,
        pointer: dict[str, Any],
        book: AcceptedBook,
    ) -> CompanionRelease:
        release_id = pointer["release_id"]
        expected_manifest = (
            self.project.releases_root / release_id / "manifest.json"
        ).relative_to(self.project.root).as_posix()
        if pointer["manifest"] != expected_manifest:
            raise CompanionReleaseError(
                "release_pointer_invalid",
                "current release manifest does not match its release ID",
            )
        release = self.validate(release_id, book)
        self._verify_delivery(release)
        return release

    def _publish_delivery(
        self,
        release: CompanionRelease,
        *,
        run_id: str,
    ) -> None:
        self.project.runtime_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(
                prefix=".delivery.",
                dir=self.project.runtime_root,
            )
        )
        try:
            staged_html = staging / "companion.html"
            staged_pdf: Path | None = None
            if release.pdf is not None:
                staged_pdf = staging / "companion.pdf"
                shutil.copyfile(release.pdf, staged_pdf)
            staged_html.write_bytes(
                _delivery_html_bytes(
                    release.web_index,
                    release.release_id,
                    _delivery_recipe_for_manifest(release.manifest),
                )
            )
            previous = self._snapshot_delivery()
            try:
                if staged_pdf is None:
                    self.project.delivery_pdf.unlink(missing_ok=True)
                else:
                    self._replace_delivery_file(
                        staged_pdf,
                        self.project.delivery_pdf,
                    )
                self._replace_delivery_file(
                    staged_html,
                    self.project.delivery_html,
                )
                self._verify_delivery(release)
                self.project.publish_current(
                    release_id=release.release_id,
                    manifest=release.manifest,
                    run_id=run_id,
                )
            except BaseException:
                self._restore_delivery(previous)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _replace_delivery_file(staged: Path, target: Path) -> None:
        atomic_write_bytes(target, staged.read_bytes())

    def _verify_delivery(self, release: CompanionRelease) -> None:
        _verify_delivery_files(
            self.project,
            release.release_id,
            release.available_formats,
            _delivery_recipe_for_manifest(release.manifest),
        )

    def _snapshot_delivery(self) -> _DeliverySnapshot:
        return _DeliverySnapshot(
            pdf=_snapshot_file(self.project.delivery_pdf),
            html=_snapshot_file(self.project.delivery_html),
            current=_snapshot_file(self.project.current),
        )

    def _restore_delivery(self, previous: _DeliverySnapshot) -> None:
        for path, snapshot in (
            (self.project.delivery_pdf, previous.pdf),
            (self.project.delivery_html, previous.html),
            (self.project.current, previous.current),
        ):
            try:
                if snapshot.existed:
                    atomic_write_bytes(path, snapshot.payload)
                else:
                    path.unlink(missing_ok=True)
            except OSError:
                # Preserve the publication failure; rollback is best effort.
                continue
        try:
            _fsync_directory(self.project.root)
        except OSError:
            pass

    def _assert_delivery_targets_known(self) -> None:
        current = self.project.current_release()
        for target, role in (
            (self.project.delivery_pdf, "pdf"),
            (self.project.delivery_html, "html"),
        ):
            if not target.exists():
                continue
            if not target.is_file():
                raise CompanionReleaseError(
                    "delivery_conflict",
                    f"project delivery target is not a file: {target.name}",
                )
            if current is None and not self._matches_known_release(
                target,
                role=role,
            ):
                raise CompanionReleaseError(
                    "delivery_conflict",
                    f"project delivery target is not managed by Companion: {target.name}",
                )

    def _matches_known_release(self, target: Path, *, role: str) -> bool:
        try:
            payload = target.read_bytes()
        except OSError:
            return False
        if not self.project.releases_root.is_dir():
            return False
        for release_dir in self.project.releases_root.iterdir():
            if not release_dir.is_dir():
                continue
            if role == "pdf":
                canonical = release_dir / "companion.pdf"
                try:
                    if canonical.read_bytes() == payload:
                        return True
                except OSError:
                    continue
            else:
                canonical = release_dir / "reader" / "index.html"
                manifest = release_dir / "manifest.json"
                if not canonical.is_file() or not manifest.is_file():
                    continue
                try:
                    expected = _delivery_html_bytes(
                        canonical,
                        release_dir.name,
                        _delivery_recipe_for_manifest(manifest),
                    )
                except CompanionReleaseError:
                    continue
                if expected == payload:
                    return True
        return False

    def _verify_existing(
        self,
        target: Path,
        *,
        release_id: str,
        book: AcceptedBook,
    ) -> CompanionRelease:
        manifest = target / "manifest.json"
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CompanionReleaseError(
                "release_invalid", "release manifest is unreadable"
            ) from exc
        if not isinstance(value, dict):
            raise CompanionReleaseError(
                "release_invalid", "release manifest has invalid fields"
            )
        schema = value.get("schema_version")
        if schema == _LEGACY_RELEASE_MANIFEST_SCHEMA:
            expected_fields = {
                "schema_version",
                "release_id",
                "identity",
                "files",
            }
            available_formats = _FULL_FORMATS
            expected_identity = _legacy_release_identity(book)
        elif schema == RELEASE_MANIFEST_SCHEMA:
            expected_fields = {
                "schema_version",
                "release_id",
                "identity",
                "available_formats",
                "files",
            }
            available_formats = _normalize_available_formats(
                value.get("available_formats"),
                code="release_invalid",
            )
            expected_identity = _release_identity_for_recipe(
                book,
                available_formats,
                _delivery_recipe_from_value(value, code="release_invalid"),
            )
        else:
            raise CompanionReleaseError(
                "release_invalid",
                "release manifest uses an unsupported schema",
            )
        if set(value) != expected_fields:
            raise CompanionReleaseError(
                "release_invalid", "release manifest has invalid fields"
            )
        if (
            value["release_id"] != release_id
            or value["identity"] != expected_identity
            or release_id != _release_id(expected_identity)
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
        pdf_path = target / "companion.pdf"
        web_index = target / "reader" / "index.html"
        if not web_index.is_file() or (
            ("pdf" in available_formats) != pdf_path.is_file()
        ):
            raise CompanionReleaseError(
                "release_invalid",
                "release files do not match its available formats",
            )
        pdf = pdf_path if "pdf" in available_formats else None
        return CompanionRelease(
            release_id,
            target,
            available_formats,
            pdf,
            web_index,
            manifest,
            True,
        )


def release_id_for(book: AcceptedBook) -> str:
    return _release_id(
        _release_identity(book, available_formats=_FULL_FORMATS)
    )


def _release_id(identity: dict[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json_bytes(identity)).hexdigest()
    return f"release-{digest[:24]}"


def _release_identity(
    book: AcceptedBook,
    *,
    available_formats: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "accepted_book_digest": book.content_digest,
        "pdf_render_recipe": PDF_RENDER_RECIPE,
        "web_render_recipe": WEB_RENDER_RECIPE,
        "validator_version": RENDER_VALIDATOR_VERSION,
        "delivery_recipe": DELIVERY_RECIPE,
        "manifest_schema": RELEASE_MANIFEST_SCHEMA,
        "available_formats": list(available_formats),
    }


def _legacy_release_identity(book: AcceptedBook) -> dict[str, str]:
    return {
        "accepted_book_digest": book.content_digest,
        "pdf_render_recipe": _PREVIOUS_PDF_RENDER_RECIPE,
        "web_render_recipe": _LEGACY_WEB_RENDER_RECIPE,
        "validator_version": _LEGACY_RENDER_VALIDATOR_VERSION,
        "delivery_recipe": _LEGACY_DELIVERY_RECIPE,
        "manifest_schema": _LEGACY_RELEASE_MANIFEST_SCHEMA,
    }


def _release_identity_for_recipe(
    book: AcceptedBook,
    available_formats: tuple[str, ...],
    delivery_recipe: str,
) -> dict[str, Any]:
    if delivery_recipe == DELIVERY_RECIPE:
        return _release_identity(book, available_formats=available_formats)
    if delivery_recipe == _FORMER_DELIVERY_RECIPE:
        return {
            "accepted_book_digest": book.content_digest,
            "pdf_render_recipe": _FORMER_PDF_RENDER_RECIPE,
            "web_render_recipe": _FORMER_WEB_RENDER_RECIPE,
            "validator_version": _FORMER_RENDER_VALIDATOR_VERSION,
            "delivery_recipe": _FORMER_DELIVERY_RECIPE,
            "manifest_schema": RELEASE_MANIFEST_SCHEMA,
            "available_formats": list(available_formats),
        }
    if delivery_recipe == _PREVIOUS_DELIVERY_RECIPE:
        return {
            "accepted_book_digest": book.content_digest,
            "pdf_render_recipe": _LAST_PDF_RENDER_RECIPE,
            "web_render_recipe": _LAST_WEB_RENDER_RECIPE,
            "validator_version": _LAST_RENDER_VALIDATOR_VERSION,
            "delivery_recipe": _PREVIOUS_DELIVERY_RECIPE,
            "manifest_schema": RELEASE_MANIFEST_SCHEMA,
            "available_formats": list(available_formats),
        }
    if delivery_recipe == _OLDER_DELIVERY_RECIPE:
        return {
            "accepted_book_digest": book.content_digest,
            "pdf_render_recipe": _LEGACY_PDF_RENDER_RECIPE,
            "web_render_recipe": _PREVIOUS_WEB_RENDER_RECIPE,
            "validator_version": _PREVIOUS_RENDER_VALIDATOR_VERSION,
            "delivery_recipe": _OLDER_DELIVERY_RECIPE,
            "manifest_schema": RELEASE_MANIFEST_SCHEMA,
            "available_formats": list(available_formats),
        }
    if delivery_recipe == _BASE_DELIVERY_RECIPE:
        return {
            "accepted_book_digest": book.content_digest,
            "pdf_render_recipe": _LEGACY_PDF_RENDER_RECIPE,
            "web_render_recipe": _LEGACY_WEB_RENDER_RECIPE,
            "validator_version": _LEGACY_RENDER_VALIDATOR_VERSION,
            "delivery_recipe": _BASE_DELIVERY_RECIPE,
            "manifest_schema": RELEASE_MANIFEST_SCHEMA,
            "available_formats": list(available_formats),
        }
    raise CompanionReleaseError(
        "release_invalid", "release manifest uses an unsupported delivery recipe"
    )


def _delivery_recipe_from_value(value: dict[str, Any], *, code: str) -> str:
    identity = value.get("identity")
    if not isinstance(identity, dict):
        raise CompanionReleaseError(code, "release manifest has invalid identity")
    recipe = identity.get("delivery_recipe")
    if recipe not in {
        _LEGACY_DELIVERY_RECIPE,
        _BASE_DELIVERY_RECIPE,
        _OLDER_DELIVERY_RECIPE,
        _PREVIOUS_DELIVERY_RECIPE,
        _FORMER_DELIVERY_RECIPE,
        DELIVERY_RECIPE,
    }:
        raise CompanionReleaseError(
            code, "release manifest uses an unsupported delivery recipe"
        )
    return recipe


def _delivery_recipe_for_manifest(manifest: Path) -> str:
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionReleaseError(
            "delivery_invalid", "release manifest is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise CompanionReleaseError("delivery_invalid", "release manifest is invalid")
    return _delivery_recipe_from_value(value, code="delivery_invalid")


def validate_current_delivery(
    project: CompanionProjectPaths,
    pointer: dict[str, Any],
) -> tuple[str, ...]:
    """Read-only validation of root projections for the pointed release."""

    release_id = pointer.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise CompanionReleaseError(
            "delivery_invalid",
            "current release has no usable delivery identity",
        )
    manifest = project.releases_root / release_id / "manifest.json"
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompanionReleaseError(
            "delivery_invalid",
            "current release manifest is unreadable",
        ) from exc
    if not isinstance(value, dict):
        raise CompanionReleaseError(
            "delivery_invalid",
            "current release manifest is invalid",
        )
    schema = value.get("schema_version")
    if schema == _LEGACY_RELEASE_MANIFEST_SCHEMA:
        available_formats = _FULL_FORMATS
    elif schema == RELEASE_MANIFEST_SCHEMA:
        available_formats = _normalize_available_formats(
            value.get("available_formats"),
            code="delivery_invalid",
        )
    else:
        raise CompanionReleaseError(
            "delivery_invalid",
            "current release manifest uses an unsupported schema",
        )
    _verify_delivery_files(
        project,
        release_id,
        available_formats,
        _delivery_recipe_from_value(value, code="delivery_invalid"),
    )
    return available_formats


def _verify_delivery_files(
    project: CompanionProjectPaths,
    release_id: str,
    available_formats: tuple[str, ...],
    delivery_recipe: str,
) -> None:
    release = project.releases_root / release_id
    canonical_pdf = release / "companion.pdf"
    canonical_html = release / "reader" / "index.html"
    try:
        expected_html = _delivery_html_bytes(
            canonical_html, release_id, delivery_recipe
        )
    except (OSError, StandaloneHtmlError) as exc:
        raise CompanionReleaseError(
            "delivery_invalid",
            "current release delivery sources are unavailable",
        ) from exc
    expected = {project.delivery_html: expected_html}
    if "pdf" in available_formats:
        try:
            expected[project.delivery_pdf] = canonical_pdf.read_bytes()
        except OSError as exc:
            raise CompanionReleaseError(
                "delivery_invalid",
                "current release PDF source is unavailable",
            ) from exc
    elif project.delivery_pdf.exists():
        raise CompanionReleaseError(
            "delivery_invalid",
            "project delivery contains a PDF not available in the current release",
        )
    for path, payload in expected.items():
        try:
            actual = path.read_bytes()
        except OSError as exc:
            raise CompanionReleaseError(
                "delivery_invalid",
                f"project delivery is missing: {path.name}",
            ) from exc
        if actual != payload:
            raise CompanionReleaseError(
                "delivery_invalid",
                f"project delivery does not match the current release: {path.name}",
            )


def _normalize_available_formats(
    value: Any,
    *,
    code: str,
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise CompanionReleaseError(
            code, "release available formats are invalid"
        )
    formats = tuple(value)
    if formats not in {_WEB_ONLY_FORMATS, _FULL_FORMATS}:
        raise CompanionReleaseError(
            code, "release available formats are invalid"
        )
    return formats


def _snapshot_file(path: Path) -> _FileSnapshot:
    if not path.exists():
        return _FileSnapshot(False)
    return _FileSnapshot(True, path.read_bytes())


def _delivery_html_bytes(
    index: Path, release_id: str, delivery_recipe: str
) -> bytes:
    if delivery_recipe in {
        DELIVERY_RECIPE,
        _FORMER_DELIVERY_RECIPE,
        _PREVIOUS_DELIVERY_RECIPE,
        _OLDER_DELIVERY_RECIPE,
    }:
        try:
            return standalone_html_bytes(index)
        except StandaloneHtmlError as exc:
            raise CompanionReleaseError(
                "delivery_invalid", str(exc)
            ) from exc
    if delivery_recipe not in {_LEGACY_DELIVERY_RECIPE, _BASE_DELIVERY_RECIPE}:
        raise CompanionReleaseError(
            "delivery_invalid", "release uses an unsupported delivery recipe"
        )
    if re.fullmatch(r"[A-Za-z0-9._-]+", release_id) is None:
        raise CompanionReleaseError(
            "delivery_invalid",
            "release ID cannot be used in the delivery base path",
        )
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CompanionReleaseError(
            "delivery_invalid",
            "canonical Web reader index is unreadable",
        ) from exc
    base = (
        f'<base href="releases/{release_id}/reader/index.html">'
    )
    folded = text.casefold()
    head_start = folded.find("<head")
    if head_start >= 0:
        insertion = text.find(">", head_start)
        if insertion < 0:
            raise CompanionReleaseError(
                "delivery_invalid",
                "canonical Web reader has a malformed head element",
            )
        return (
            text[: insertion + 1]
            + "\n  "
            + base
            + text[insertion + 1 :]
        ).encode("utf-8")
    html_start = folded.find("<html")
    if html_start < 0:
        raise CompanionReleaseError(
            "delivery_invalid",
            "canonical Web reader has no html element",
        )
    insertion = text.find(">", html_start)
    if insertion < 0:
        raise CompanionReleaseError(
            "delivery_invalid",
            "canonical Web reader has a malformed html element",
        )
    return (
        text[: insertion + 1]
        + "\n<head>\n  "
        + base
        + "\n</head>"
        + text[insertion + 1 :]
    ).encode("utf-8")


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
    if _WINDOWS:  # pragma: no cover - exercised through platform simulation
        return
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
    "validate_current_delivery",
]
