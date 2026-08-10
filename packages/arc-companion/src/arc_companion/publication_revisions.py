"""Atomic, replayable operator revisions for published Companions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import atomic_write_bytes, canonical_json_bytes
from arc_render import (
    FragmentRevision,
    decode_fragment_revision,
    encode_fragment_revision,
    extract_markdown_citation_ids,
    fragment_revision_storage_path,
    publication_edition_digest,
    read_publication_workspace_state,
    read_publication,
    resolve_fragment_revisions,
)

from .project import CompanionProjectPaths
from .rich_text import RichTextError, parse_markdown


PUBLICATION_REVISION_REQUEST_SCHEMA = (
    "arc.companion.publication_revision_request.v1"
)
PUBLICATION_REVISION_RESULT_SCHEMA = (
    "arc.companion.publication_revision_result.v1"
)
PUBLICATION_REVISION_BUNDLE_SCHEMA = (
    "arc.companion.publication_revision_bundle.v1"
)
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_REQUEST_FIELDS = {
    "schema_version",
    "run_id",
    "publication_digest",
    "review_id",
    "reason",
    "reviewer",
    "replacements",
}
_REPLACEMENT_FIELDS = {
    "fragment_id",
    "base_semantic_digest",
    "title",
    "markdown_body",
}
_BUNDLE_FIELDS = {
    "schema_version",
    "request_digest",
    "run_id",
    "publication_digest",
    "review_id",
    "revision_files",
    "new_revision_digests",
    "selected_revision_digests",
    "edition_digest",
}


class CompanionPublicationRevisionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CompanionFragmentReplacement:
    fragment_id: str
    base_semantic_digest: str
    title: str | None
    markdown_body: str

    def __post_init__(self) -> None:
        _require_identifier(self.fragment_id, "fragment_id")
        _require_digest(self.base_semantic_digest, "base_semantic_digest")
        if self.title is not None and (
            not isinstance(self.title, str) or not self.title.strip()
        ):
            raise ValueError("replacement title must be null or non-empty text")
        if not isinstance(self.markdown_body, str) or not self.markdown_body.strip():
            raise ValueError("replacement markdown_body must be non-empty text")


@dataclass(frozen=True)
class CompanionPublicationRevisionRequest:
    run_id: str
    publication_digest: str
    review_id: str
    reason: str
    replacements: tuple[CompanionFragmentReplacement, ...]
    reviewer: str | None = None
    schema_version: str = PUBLICATION_REVISION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_REVISION_REQUEST_SCHEMA:
            raise ValueError("unsupported publication revision request schema")
        _require_run_id(self.run_id)
        _require_digest(self.publication_digest, "publication_digest")
        _require_identifier(self.review_id, "review_id")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("revision reason must be non-empty text")
        if self.reviewer is not None and (
            not isinstance(self.reviewer, str) or not self.reviewer.strip()
        ):
            raise ValueError("reviewer must be null or non-empty text")
        replacements = tuple(self.replacements)
        if not replacements or any(
            not isinstance(item, CompanionFragmentReplacement)
            for item in replacements
        ):
            raise ValueError("revision request requires replacements")
        ids = tuple(item.fragment_id for item in replacements)
        if len(set(ids)) != len(ids):
            raise ValueError("revision request repeats a fragment ID")
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(
            self,
            "reviewer",
            None if self.reviewer is None else self.reviewer.strip(),
        )
        object.__setattr__(self, "replacements", replacements)

    @property
    def request_digest(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(encode_publication_revision_request(self))
        ).hexdigest()


@dataclass(frozen=True)
class CompanionPublicationRevisionResult:
    request_digest: str
    review_id: str
    revision_digests: tuple[str, ...]
    selected_revision_digests: tuple[str, ...]
    edition_digest: str
    idempotent_replay: bool
    html_path: str
    schema_version: str = PUBLICATION_REVISION_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != PUBLICATION_REVISION_RESULT_SCHEMA:
            raise ValueError("unsupported publication revision result schema")
        _require_digest(self.request_digest, "request_digest")
        _require_identifier(self.review_id, "review_id")
        _require_digests(self.revision_digests, "revision_digests", nonempty=True)
        _require_digests(
            self.selected_revision_digests,
            "selected_revision_digests",
            nonempty=False,
        )
        _require_digest(self.edition_digest, "edition_digest")
        if not isinstance(self.idempotent_replay, bool):
            raise ValueError("idempotent_replay must be boolean")
        if not isinstance(self.html_path, str) or not self.html_path:
            raise ValueError("html_path must be non-empty text")


def encode_publication_revision_request(
    request: CompanionPublicationRevisionRequest,
) -> dict[str, Any]:
    if not isinstance(request, CompanionPublicationRevisionRequest):
        raise TypeError("request must be CompanionPublicationRevisionRequest")
    return {
        "schema_version": request.schema_version,
        "run_id": request.run_id,
        "publication_digest": request.publication_digest,
        "review_id": request.review_id,
        "reason": request.reason,
        "reviewer": request.reviewer,
        "replacements": [
            {
                "fragment_id": item.fragment_id,
                "base_semantic_digest": item.base_semantic_digest,
                "title": item.title,
                "markdown_body": item.markdown_body,
            }
            for item in request.replacements
        ],
    }


def decode_publication_revision_request(
    value: Mapping[str, Any],
) -> CompanionPublicationRevisionRequest:
    try:
        if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
            raise ValueError("revision request has invalid fields")
        raw_replacements = value["replacements"]
        if not isinstance(raw_replacements, list):
            raise ValueError("replacements must be an array")
        replacements = []
        for raw in raw_replacements:
            if not isinstance(raw, Mapping) or set(raw) != _REPLACEMENT_FIELDS:
                raise ValueError("replacement has invalid fields")
            replacements.append(
                CompanionFragmentReplacement(
                    fragment_id=raw["fragment_id"],
                    base_semantic_digest=raw["base_semantic_digest"],
                    title=raw["title"],
                    markdown_body=raw["markdown_body"],
                )
            )
        return CompanionPublicationRevisionRequest(
            run_id=value["run_id"],
            publication_digest=value["publication_digest"],
            review_id=value["review_id"],
            reason=value["reason"],
            reviewer=value["reviewer"],
            replacements=tuple(replacements),
            schema_version=value["schema_version"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CompanionPublicationRevisionError(
            "publication_revision_request_invalid",
            f"publication revision request is invalid: {exc}",
        ) from exc


def encode_publication_revision_result(
    result: CompanionPublicationRevisionResult,
) -> dict[str, Any]:
    return {
        "schema_version": result.schema_version,
        "request_digest": result.request_digest,
        "review_id": result.review_id,
        "revision_digests": list(result.revision_digests),
        "selected_revision_digests": list(result.selected_revision_digests),
        "edition_digest": result.edition_digest,
        "idempotent_replay": result.idempotent_replay,
        "html_path": result.html_path,
    }


def materialize_operator_revisions(
    paths: CompanionProjectPaths,
    run_id: str,
    workspace: str | Path,
) -> tuple[str, ...]:
    """Replay every committed bundle into a publication workspace."""

    root = paths.operator_revisions_run_path(run_id)
    workspace_root = Path(workspace).resolve()
    publication_path = workspace_root / "publication.json"
    if not publication_path.is_file():
        raise CompanionPublicationRevisionError(
            "publication_revision_workspace_invalid",
            "operator revisions require a materialized publication",
        )
    publication_digest = read_publication(publication_path).publication_digest
    review_ids: list[str] = []
    new_revision_digests: set[str] = set()
    if not root.is_dir():
        return ()
    for bundle in sorted(root.glob("review-*")):
        if not bundle.is_dir():
            continue
        manifest = _read_bundle(bundle)
        if manifest["run_id"] != run_id:
            raise CompanionPublicationRevisionError(
                "publication_revision_bundle_invalid",
                "committed review bundle belongs to another run",
            )
        if manifest["publication_digest"] != publication_digest:
            raise CompanionPublicationRevisionError(
                "publication_revision_publication_mismatch",
                "committed review bundle belongs to another publication",
            )
        review_ids.append(manifest["review_id"])
        new_revision_digests.update(manifest["new_revision_digests"])
        for entry in manifest["revision_files"]:
            source = bundle / entry["path"]
            target = workspace_root / entry["path"]
            payload = source.read_bytes()
            _validate_revision_payload(payload, entry)
            _write_exact(target, payload)
    if len(set(review_ids)) != len(review_ids):
        raise CompanionPublicationRevisionError(
            "publication_revision_review_id_conflict",
            "committed review bundles repeat a review ID",
        )
    state = read_publication_workspace_state(publication_path)
    conflict = next(
        (item for item in state.diagnostics if item.severity == "conflict"),
        None,
    )
    if conflict is not None:
        raise CompanionPublicationRevisionError(
            "publication_revision_history_conflict",
            f"operator revisions create a history conflict: {conflict.message}",
        )
    ancestry = _selected_ancestry_digests(
        state.revisions, state.selected_revisions
    )
    if not new_revision_digests.issubset(ancestry):
        raise CompanionPublicationRevisionError(
            "publication_revision_history_conflict",
            "a committed operator revision is not in the selected lineage",
        )
    return tuple(review_ids)


def committed_publication_review_ids(
    paths: CompanionProjectPaths, run_id: str
) -> tuple[str, ...]:
    root = paths.operator_revisions_run_path(run_id)
    if not root.is_dir():
        return ()
    return tuple(
        _read_bundle(bundle)["review_id"]
        for bundle in sorted(root.glob("review-*"))
        if bundle.is_dir()
    )


def commit_publication_revision(
    paths: CompanionProjectPaths,
    request: CompanionPublicationRevisionRequest,
    publication_path: str | Path,
) -> CompanionPublicationRevisionResult:
    """Commit one review bundle; caller must hold the delivery lease."""

    if paths.current_run_id != request.run_id:
        raise CompanionPublicationRevisionError(
            "publication_revision_run_mismatch",
            "revision request does not target the selected run",
        )
    publication_path = Path(publication_path).resolve()
    state = read_publication_workspace_state(publication_path)
    if state.publication_digest != request.publication_digest:
        raise CompanionPublicationRevisionError(
            "publication_revision_publication_mismatch",
            "revision request does not target the selected publication",
        )
    run_root = paths.operator_revisions_run_path(request.run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    _cleanup_staging(run_root)
    bundle = run_root / f"review-{request.request_digest}"
    if bundle.is_dir():
        manifest = _read_bundle(bundle)
        expected = canonical_json_bytes(
            encode_publication_revision_request(request)
        )
        if (bundle / "request.json").read_bytes() != expected:
            raise CompanionPublicationRevisionError(
                "publication_revision_bundle_invalid",
                "existing review bundle request bytes are inconsistent",
            )
        materialize_operator_revisions(
            paths, request.run_id, publication_path.parent
        )
        current = read_publication_workspace_state(publication_path)
        return _result_from_state(
            paths,
            request,
            manifest["new_revision_digests"],
            current,
            idempotent=True,
        )
    if bundle.exists():
        raise CompanionPublicationRevisionError(
            "publication_revision_bundle_invalid",
            "review bundle path is not a directory",
        )
    for existing in sorted(run_root.glob("review-*")):
        if not existing.is_dir():
            continue
        manifest = _read_bundle(existing)
        if manifest["review_id"] == request.review_id:
            raise CompanionPublicationRevisionError(
                "publication_revision_review_id_conflict",
                "review ID is already bound to another request",
            )

    conflict = next(
        (item for item in state.diagnostics if item.severity == "conflict"),
        None,
    )
    if conflict is not None:
        raise CompanionPublicationRevisionError(
            "publication_revision_history_conflict",
            f"publication revision history is conflicted: {conflict.message}",
        )
    selected = {item.fragment_id: item for item in state.selected_revisions}
    bibliography_ids = _bibliography_ids(state.publication.bibliography)
    new_revisions: list[FragmentRevision] = []
    for replacement in request.replacements:
        base = selected.get(replacement.fragment_id)
        if base is None:
            raise CompanionPublicationRevisionError(
                "publication_revision_fragment_unknown",
                f"unknown selected fragment: {replacement.fragment_id}",
            )
        if base.semantic_digest != replacement.base_semantic_digest:
            raise CompanionPublicationRevisionError(
                "publication_revision_stale_base",
                f"fragment base is stale: {replacement.fragment_id}",
            )
        try:
            parse_markdown(replacement.markdown_body)
        except RichTextError as exc:
            raise CompanionPublicationRevisionError(
                "publication_revision_markdown_invalid",
                f"replacement Markdown is invalid: {replacement.fragment_id}",
            ) from exc
        citation_ids = extract_markdown_citation_ids(
            replacement.markdown_body
        )
        unknown = next(
            (item for item in citation_ids if item not in bibliography_ids),
            None,
        )
        if unknown is not None:
            raise CompanionPublicationRevisionError(
                "publication_revision_citation_unknown",
                f"replacement cites an unknown bibliography ID: {unknown}",
            )
        provenance = dict(base.provenance)
        review = {
            "review_id": request.review_id,
            "reason": request.reason,
            "base_semantic_digest": base.semantic_digest,
        }
        if request.reviewer is not None:
            review["reviewer"] = request.reviewer
        provenance.update(
            {
                "last_editor": "arc-companion-review",
                "publication_review": review,
            }
        )
        revision = FragmentRevision(
            source=base.source,
            fragment_id=base.fragment_id,
            revision=base.revision + 1,
            parent_semantic_digest=base.semantic_digest,
            anchor=base.anchor,
            priority=base.priority,
            role=base.role,
            language=base.language,
            title=replacement.title,
            citation_ids=citation_ids,
            provenance=provenance,
            markdown_body=replacement.markdown_body,
        )
        if (
            revision.title == base.title
            and revision.markdown_body == base.markdown_body
        ):
            raise CompanionPublicationRevisionError(
                "publication_revision_no_change",
                f"replacement makes no visible change: {replacement.fragment_id}",
            )
        new_revisions.append(revision)

    affected = {item.fragment_id for item in new_revisions}
    bases = tuple(
        item for item in state.selected_revisions if item.fragment_id in affected
    )
    known_digests = _known_durable_revision_digests(run_root, state.revisions)
    adopted = _selected_ancestry(
        state.revisions,
        state.selected_revisions,
        affected,
        excluded_digests=known_digests,
    )
    expected_selected = tuple(
        next(
            (
                child.semantic_digest
                for child in new_revisions
                if child.fragment_id == current.fragment_id
            ),
            current.semantic_digest,
        )
        for current in state.selected_revisions
    )
    edition_digest = publication_edition_digest(
        state.publication_digest, expected_selected
    )
    manifest = {
        "schema_version": PUBLICATION_REVISION_BUNDLE_SCHEMA,
        "request_digest": request.request_digest,
        "run_id": request.run_id,
        "publication_digest": request.publication_digest,
        "review_id": request.review_id,
        "revision_files": [],
        "new_revision_digests": [
            item.semantic_digest for item in new_revisions
        ],
        "selected_revision_digests": list(expected_selected),
        "edition_digest": edition_digest,
    }
    all_revisions = (*bases, *adopted, *new_revisions)
    seen_digests: set[str] = set()
    for revision in all_revisions:
        if revision.semantic_digest in seen_digests:
            continue
        seen_digests.add(revision.semantic_digest)
        relative = fragment_revision_storage_path(revision)
        manifest["revision_files"].append(
            {
                "path": relative,
                "fragment_id": revision.fragment_id,
                "revision": revision.revision,
                "semantic_digest": revision.semantic_digest,
                "adopted": revision not in new_revisions,
            }
        )
    manifest["revision_files"].sort(key=lambda item: item["path"])

    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=run_root))
    try:
        atomic_write_bytes(
            staging / "request.json",
            canonical_json_bytes(encode_publication_revision_request(request)),
        )
        for revision in all_revisions:
            target = staging / fragment_revision_storage_path(revision)
            _write_exact(target, encode_fragment_revision(revision).encode("utf-8"))
        atomic_write_bytes(staging / "result.json", canonical_json_bytes(manifest))
        checked = _read_bundle(staging, require_canonical_name=False)
        if checked != manifest:
            raise CompanionPublicationRevisionError(
                "publication_revision_bundle_invalid",
                "staged review bundle failed byte validation",
            )
        _validate_new_lineages(state.revisions, new_revisions)
        _fsync_directory(staging / "fragments")
        _fsync_directory(staging)
        staging.rename(bundle)
        _fsync_directory(run_root)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    materialize_operator_revisions(paths, request.run_id, publication_path.parent)
    current = read_publication_workspace_state(publication_path)
    if current.selected_revision_digests != expected_selected:
        raise CompanionPublicationRevisionError(
            "publication_revision_commit_invalid",
            "committed review did not select the expected revision heads",
        )
    return _result_from_state(
        paths,
        request,
        tuple(item.semantic_digest for item in new_revisions),
        current,
        idempotent=False,
    )


def _result_from_state(
    paths: CompanionProjectPaths,
    request: CompanionPublicationRevisionRequest,
    revision_digests: Sequence[str],
    state: Any,
    *,
    idempotent: bool,
) -> CompanionPublicationRevisionResult:
    return CompanionPublicationRevisionResult(
        request_digest=request.request_digest,
        review_id=request.review_id,
        revision_digests=tuple(revision_digests),
        selected_revision_digests=state.selected_revision_digests,
        edition_digest=state.edition_digest,
        idempotent_replay=idempotent,
        html_path=str(paths.delivery_html),
    )


def _selected_ancestry(
    revisions: Sequence[FragmentRevision],
    selected: Sequence[FragmentRevision],
    affected: set[str],
    *,
    excluded_digests: set[str] | None = None,
) -> tuple[FragmentRevision, ...]:
    by_digest = {item.semantic_digest: item for item in revisions}
    values: list[FragmentRevision] = []
    for head in selected:
        if head.fragment_id not in affected:
            continue
        current: FragmentRevision | None = head
        chain: list[FragmentRevision] = []
        while current is not None:
            chain.append(current)
            parent = current.parent_semantic_digest
            current = None if parent is None else by_digest.get(parent)
            if parent is not None and current is None:
                raise CompanionPublicationRevisionError(
                    "publication_revision_history_invalid",
                    f"selected fragment has a missing ancestor: {head.fragment_id}",
                )
        values.extend(
            item
            for item in reversed(chain)
            if item.semantic_digest not in (excluded_digests or set())
        )
    return tuple(values)


def _known_durable_revision_digests(
    run_root: Path,
    revisions: Sequence[FragmentRevision],
) -> set[str]:
    known = {
        item.semantic_digest
        for item in revisions
        if item.revision == 1
        and item.provenance.get("producer") != "arc-render-browser"
    }
    if not run_root.is_dir():
        return known
    for bundle in sorted(run_root.glob("review-*")):
        if not bundle.is_dir():
            continue
        manifest = _read_bundle(bundle)
        known.update(
            entry["semantic_digest"]
            for entry in manifest["revision_files"]
        )
    return known


def _validate_new_lineages(
    current: Sequence[FragmentRevision],
    new_revisions: Sequence[FragmentRevision],
) -> None:
    for child in new_revisions:
        resolution = resolve_fragment_revisions(
            (
                *(item for item in current if item.fragment_id == child.fragment_id),
                child,
            )
        )
        if (
            resolution.has_conflict
            or resolution.selected_digest != child.semantic_digest
        ):
            raise CompanionPublicationRevisionError(
                "publication_revision_fork",
                f"revision would create a fork: {child.fragment_id}",
            )


def _read_bundle(
    bundle: Path, *, require_canonical_name: bool = True
) -> dict[str, Any]:
    try:
        value = json.loads((bundle / "result.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != _BUNDLE_FIELDS:
            raise ValueError("bundle manifest has invalid fields")
        if value["schema_version"] != PUBLICATION_REVISION_BUNDLE_SCHEMA:
            raise ValueError("bundle manifest has unsupported schema")
        if (bundle / "result.json").read_bytes() != canonical_json_bytes(value):
            raise ValueError("bundle manifest is not canonical")
        _require_digest(value["request_digest"], "request_digest")
        _require_run_id(value["run_id"])
        _require_digest(value["publication_digest"], "publication_digest")
        _require_identifier(value["review_id"], "review_id")
        _require_digests(
            value["new_revision_digests"],
            "new_revision_digests",
            nonempty=True,
        )
        _require_digests(
            value["selected_revision_digests"],
            "selected_revision_digests",
            nonempty=False,
        )
        _require_digest(value["edition_digest"], "edition_digest")
        if (
            require_canonical_name
            and bundle.name != f"review-{value['request_digest']}"
        ):
            raise ValueError("bundle directory name does not match request digest")
        entries = value["revision_files"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("bundle revision_files must be non-empty")
        paths: set[str] = set()
        digests: set[str] = set()
        revisions: list[tuple[FragmentRevision, dict[str, Any]]] = []
        for entry in entries:
            expected_fields = {
                "path",
                "fragment_id",
                "revision",
                "semantic_digest",
                "adopted",
            }
            if not isinstance(entry, dict) or set(entry) != expected_fields:
                raise ValueError("bundle revision entry has invalid fields")
            path = entry["path"]
            if (
                not isinstance(path, str)
                or not path.startswith("fragments/")
                or Path(path).is_absolute()
                or ".." in Path(path).parts
                or path in paths
            ):
                raise ValueError("bundle revision path is invalid")
            paths.add(path)
            _require_identifier(entry["fragment_id"], "fragment_id")
            if (
                not isinstance(entry["revision"], int)
                or isinstance(entry["revision"], bool)
                or entry["revision"] < 1
            ):
                raise ValueError("bundle revision number is invalid")
            _require_digest(entry["semantic_digest"], "semantic_digest")
            if entry["semantic_digest"] in digests:
                raise ValueError("bundle repeats a revision digest")
            digests.add(entry["semantic_digest"])
            if not isinstance(entry["adopted"], bool):
                raise ValueError("bundle adopted marker is invalid")
            payload = (bundle / path).read_bytes()
            _validate_revision_payload(payload, entry)
            revisions.append(
                (
                    decode_fragment_revision(
                        payload.decode("utf-8"), filename=Path(path).name
                    ),
                    entry,
                )
            )
        request_bytes = (bundle / "request.json").read_bytes()
        request_value = json.loads(request_bytes.decode("utf-8"))
        request = decode_publication_revision_request(request_value)
        if (
            request.request_digest != value["request_digest"]
            or request.run_id != value["run_id"]
            or request.publication_digest != value["publication_digest"]
            or request.review_id != value["review_id"]
            or request_bytes
            != canonical_json_bytes(encode_publication_revision_request(request))
        ):
            raise ValueError("bundle request does not match its manifest")
        if not set(value["new_revision_digests"]).issubset(digests):
            raise ValueError("bundle omits a new revision")
        _validate_bundle_request_children(
            request,
            revisions,
            tuple(value["new_revision_digests"]),
        )
        expected_edition = publication_edition_digest(
            value["publication_digest"], value["selected_revision_digests"]
        )
        if expected_edition != value["edition_digest"]:
            raise ValueError("bundle edition digest is invalid")
        return value
    except CompanionPublicationRevisionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CompanionPublicationRevisionError(
            "publication_revision_bundle_invalid",
            f"committed review bundle is invalid: {exc}",
        ) from exc


def _validate_revision_payload(payload: bytes, entry: Mapping[str, Any]) -> None:
    revision = decode_fragment_revision(
        payload.decode("utf-8"), filename=Path(entry["path"]).name
    )
    if (
        revision.fragment_id != entry["fragment_id"]
        or revision.revision != entry["revision"]
        or revision.semantic_digest != entry["semantic_digest"]
    ):
        raise ValueError("bundle revision bytes do not match their manifest")


def _validate_bundle_request_children(
    request: CompanionPublicationRevisionRequest,
    revisions: Sequence[tuple[FragmentRevision, Mapping[str, Any]]],
    new_revision_digests: tuple[str, ...],
) -> None:
    by_digest = {item.semantic_digest: item for item, _entry in revisions}
    children = [
        (item, entry)
        for item, entry in revisions
        if entry["adopted"] is False
    ]
    if len(children) != len(request.replacements):
        raise ValueError("bundle new revisions do not match request replacements")
    child_by_fragment = {item.fragment_id: (item, entry) for item, entry in children}
    if len(child_by_fragment) != len(children):
        raise ValueError("bundle repeats a requested child fragment")
    expected_digests: list[str] = []
    for replacement in request.replacements:
        try:
            child, _entry = child_by_fragment[replacement.fragment_id]
            base = by_digest[replacement.base_semantic_digest]
        except KeyError as exc:
            raise ValueError("bundle omits a requested child or its base") from exc
        citations = extract_markdown_citation_ids(replacement.markdown_body)
        provenance = dict(base.provenance)
        review: dict[str, Any] = {
            "review_id": request.review_id,
            "reason": request.reason,
            "base_semantic_digest": base.semantic_digest,
        }
        if request.reviewer is not None:
            review["reviewer"] = request.reviewer
        provenance.update(
            {
                "last_editor": "arc-companion-review",
                "publication_review": review,
            }
        )
        expected = FragmentRevision(
            source=base.source,
            fragment_id=base.fragment_id,
            revision=base.revision + 1,
            parent_semantic_digest=base.semantic_digest,
            anchor=base.anchor,
            priority=base.priority,
            role=base.role,
            language=base.language,
            title=replacement.title,
            citation_ids=citations,
            provenance=provenance,
            markdown_body=replacement.markdown_body,
        )
        if child != expected:
            raise ValueError("bundle child does not match its canonical request")
        expected_digests.append(expected.semantic_digest)
    if tuple(expected_digests) != new_revision_digests:
        raise ValueError("bundle new revision digests are not request ordered")


def _selected_ancestry_digests(
    revisions: Sequence[FragmentRevision],
    selected: Sequence[FragmentRevision],
) -> set[str]:
    by_digest = {item.semantic_digest: item for item in revisions}
    result: set[str] = set()
    for head in selected:
        current: FragmentRevision | None = head
        while current is not None and current.semantic_digest not in result:
            result.add(current.semantic_digest)
            parent = current.parent_semantic_digest
            current = None if parent is None else by_digest.get(parent)
            if parent is not None and current is None:
                raise CompanionPublicationRevisionError(
                    "publication_revision_history_invalid",
                    f"selected fragment has a missing ancestor: {head.fragment_id}",
                )
    return result


def _bibliography_ids(values: Sequence[Mapping[str, Any]]) -> set[str]:
    result = {
        str(
            item.get("evidence_id")
            or item.get("citation_id")
            or item.get("id")
            or ""
        )
        for item in values
    }
    result.discard("")
    return result


def _cleanup_staging(run_root: Path) -> None:
    for candidate in run_root.glob(".staging-*"):
        if candidate.is_dir() and candidate.parent == run_root:
            shutil.rmtree(candidate)


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_file() and path.read_bytes() == payload:
            return
        raise CompanionPublicationRevisionError(
            "publication_revision_workspace_conflict",
            f"revision path contains conflicting bytes: {path}",
        )
    atomic_write_bytes(path, payload)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return value


def _require_run_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("run_id must be a local identifier")
    return value


def _require_digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _require_digests(
    values: Any, name: str, *, nonempty: bool
) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or (nonempty and not values):
        raise ValueError(f"{name} must be an array of digests")
    result = tuple(_require_digest(item, name) for item in values)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not repeat digests")
    return result


__all__ = [
    "PUBLICATION_REVISION_BUNDLE_SCHEMA",
    "PUBLICATION_REVISION_REQUEST_SCHEMA",
    "PUBLICATION_REVISION_RESULT_SCHEMA",
    "CompanionFragmentReplacement",
    "CompanionPublicationRevisionError",
    "CompanionPublicationRevisionRequest",
    "CompanionPublicationRevisionResult",
    "commit_publication_revision",
    "committed_publication_review_ids",
    "decode_publication_revision_request",
    "encode_publication_revision_request",
    "encode_publication_revision_result",
    "materialize_operator_revisions",
]
