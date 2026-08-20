"""Validate and expose native arc-translate selection results to Companion."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import ArcJobsError, RunContext
from arc_document import RichBlock, RichBlockKind, RichDocument
from arc_render import (
    AnchorKind,
    FragmentRevision,
    anchor_block_from_rich_block,
    decode_fragment_revision,
    source_identity_from_rich_document,
)
from arc_translate import TranslationResult


class CompanionTranslationResultError(ValueError):
    """A chapter translation result is not usable as a native render layer."""


@dataclass(frozen=True)
class ValidatedTranslationSelection:
    result: TranslationResult
    revisions: tuple[FragmentRevision, ...]
    view_records: tuple[Mapping[str, str], ...]


def load_translation_selection(
    context: RunContext,
    value: Mapping[str, Any],
    *,
    source: RichDocument,
    block_ids: Sequence[str],
    target_language: str,
) -> ValidatedTranslationSelection:
    """Load one selected-block result and verify every immutable revision."""

    try:
        result = TranslationResult.from_document(value)
    except (TypeError, ValueError) as exc:
        raise CompanionTranslationResultError(
            "arc-translate returned an invalid native result"
        ) from exc
    source_identity = source_identity_from_rich_document(source)
    if (
        result.mode != "enabled"
        or result.coverage != "selection"
        or result.target_language != target_language
        or result.layer.source != source_identity
        or result.layer.producer != "arc-translate"
    ):
        raise CompanionTranslationResultError(
            "arc-translate result does not match the Companion chapter"
        )

    blocks = {item.block_id: item for item in source.blocks}
    try:
        ordered_blocks = tuple(blocks[block_id] for block_id in block_ids)
    except KeyError as exc:
        raise CompanionTranslationResultError(
            "chapter translation refers to an unknown source block"
        ) from exc
    expected = tuple(
        block.block_id
        for block in ordered_blocks
        if not _non_language_figure(block)
    )
    revisions: list[FragmentRevision] = []
    try:
        for item in result.revision_artifacts:
            payload = context.artifacts.read_bytes(item.artifact)
            revision = decode_fragment_revision(
                payload.decode("utf-8"),
                filename=Path(item.revision.path).name,
            )
            if (
                revision.fragment_id != item.revision.fragment_id
                or revision.revision != item.revision.revision
                or revision.semantic_digest != item.revision.semantic_digest
                or revision.source != source_identity
                or revision.priority != 10
                or revision.role != "translation"
                or revision.language != target_language
                or revision.title is not None
                or revision.citation_ids
                or revision.provenance.get("producer") != "arc-translate"
                or revision.anchor.kind is not AnchorKind.BLOCK
            ):
                raise ValueError("translation revision identity mismatch")
            block = blocks.get(revision.anchor.target_id)
            if (
                block is None
                or revision.anchor.related_blocks
                != (anchor_block_from_rich_block(block),)
            ):
                raise ValueError("translation revision anchor mismatch")
            revisions.append(revision)
    except (ArcJobsError, OSError, UnicodeError, ValueError) as exc:
        raise CompanionTranslationResultError(
            "arc-translate revision artifact is invalid"
        ) from exc
    if tuple(item.anchor.target_id for item in revisions) != expected:
        raise CompanionTranslationResultError(
            "arc-translate did not exactly cover the chapter source order"
        )

    by_block = {item.anchor.target_id: item for item in revisions}
    records = tuple(
        {
            "block_id": block.block_id,
            "text": (
                "\ufffc"
                if _non_language_figure(block)
                else by_block[block.block_id].markdown_body
            ),
        }
        for block in ordered_blocks
    )
    return ValidatedTranslationSelection(
        result,
        tuple(revisions),
        records,
    )


def _non_language_figure(block: RichBlock) -> bool:
    return (
        block.kind is RichBlockKind.FIGURE
        and not str(block.payload["caption"]).strip()
        and not str(block.payload["alt_text"]).strip()
    )


__all__ = [
    "CompanionTranslationResultError",
    "ValidatedTranslationSelection",
    "load_translation_selection",
]
