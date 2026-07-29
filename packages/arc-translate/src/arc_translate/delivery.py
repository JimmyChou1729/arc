"""Publish native arc-render translation fragments and their Layer."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from arc_render import (
    AnchorKind,
    FragmentRevision,
    decode_fragment_revision,
    fragment_revision_filename,
    read_fragment_revision,
    read_layer,
    relative_fragment_path,
    write_fragment_revision,
    write_layer,
)

from .project import TranslationProject
from .workflow import TranslationResult


class TranslationDeliveryError(RuntimeError):
    code = "translation_delivery_invalid"


def publish_translation_layer(
    project: TranslationProject,
    *,
    result: TranslationResult,
    revision_payloads: Sequence[bytes],
) -> Path:
    """Publish immutable revision files followed by their atomic Layer."""

    if not isinstance(result, TranslationResult):
        raise TypeError("result must be a TranslationResult")
    if result.coverage != "document":
        raise TranslationDeliveryError(
            "a selected-block translation cannot replace the document Layer"
        )
    payloads = tuple(revision_payloads)
    if len(payloads) != len(result.revision_artifacts):
        raise TranslationDeliveryError(
            "translation revision payload count does not match the result manifest"
        )
    revisions = _validated_revisions(result, payloads)
    for item, revision in zip(
        result.revision_artifacts, revisions, strict=True
    ):
        try:
            published = write_fragment_revision(project.root, revision)
            if (
                relative_fragment_path(project.root, published)
                != item.revision.path
            ):
                raise ValueError("revision path mismatch")
        except (OSError, UnicodeError, ValueError) as exc:
            raise TranslationDeliveryError(
                "translation revision payload is invalid"
            ) from exc
    try:
        write_layer(project.translation_layer, result.layer)
    except (OSError, ValueError) as exc:
        raise TranslationDeliveryError(
            "translation Layer could not be published"
        ) from exc
    validate_translation_layer(project, result=result)
    return project.translation_layer


def _validated_revisions(
    result: TranslationResult,
    payloads: Sequence[bytes],
) -> tuple[FragmentRevision, ...]:
    revisions: list[FragmentRevision] = []
    try:
        for item, payload in zip(
            result.revision_artifacts, payloads, strict=True
        ):
            revision = decode_fragment_revision(
                payload.decode("utf-8"),
                filename=Path(item.revision.path).name,
            )
            if (
                revision.fragment_id != item.revision.fragment_id
                or revision.revision != item.revision.revision
                or revision.semantic_digest != item.revision.semantic_digest
                or revision.source != result.layer.source
                or revision.priority != 10
                or revision.role != "translation"
                or revision.language != result.target_language
                or revision.anchor.kind is not AnchorKind.BLOCK
                or revision.title is not None
                or revision.citation_ids
                or revision.provenance.get("producer") != "arc-translate"
                or item.revision.path
                != (
                    f"fragments/{revision.fragment_id}/"
                    f"{fragment_revision_filename(revision)}"
                )
            ):
                raise ValueError(
                    "revision does not match the translation result"
                )
            revisions.append(revision)
    except (OSError, UnicodeError, ValueError) as exc:
        raise TranslationDeliveryError(
            "translation revision payload is invalid"
        ) from exc
    return tuple(revisions)


def validate_translation_layer(
    project: TranslationProject,
    *,
    result: TranslationResult,
) -> None:
    """Validate a visible Layer and every referenced immutable revision."""

    try:
        layer = read_layer(project.translation_layer)
        if layer != result.layer:
            raise ValueError("published Layer does not match the result")
        for reference in layer.initial_revisions:
            revision = read_fragment_revision(project.root / reference.path)
            if (
                revision.fragment_id != reference.fragment_id
                or revision.semantic_digest != reference.semantic_digest
                or revision.source != layer.source
                or revision.priority != 10
                or revision.role != "translation"
                or revision.language != result.target_language
            ):
                raise ValueError("published revision does not match its Layer")
    except (OSError, UnicodeError, ValueError) as exc:
        raise TranslationDeliveryError(
            "translation Layer delivery is unreadable or invalid"
        ) from exc


__all__ = [
    "TranslationDeliveryError",
    "publish_translation_layer",
    "validate_translation_layer",
]
