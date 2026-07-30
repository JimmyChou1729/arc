"""Build and materialize ARC Render publications from Companion results."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arc_jobs import (
    ArcJobsError,
    ArtifactRef,
    ImmutableArtifactStore,
    RunContext,
    atomic_write_bytes,
    decode_artifact_ref,
)
from arc_paper import (
    ArcPaperService,
    RichDocument,
    SourceRepositoryError,
)
from arc_render import (
    AnchorKind,
    FragmentAnchor,
    FragmentRevision,
    FragmentRevisionRef,
    Layer,
    Publication,
    PublicationOutlineItem,
    anchor_block_from_rich_block,
    encode_fragment_revision,
    fragment_revision_ref,
    fragment_revision_storage_path,
    layer_from_document,
    layer_to_document,
    normalize_markdown,
    publication_from_document,
    publication_to_document,
)

from ._build_support import ref_document
from .translation_results import (
    CompanionTranslationResultError,
    load_translation_selection,
)


BUILD_RESULT_SCHEMA = "arc.companion.build_result.v2"
PUBLICATION_ARTIFACT = "publication/publication.json"
TRANSLATION_LAYER_ARTIFACT = "publication/layers/translation.json"
COMPANION_LAYER_ARTIFACT = "publication/layers/companion.json"


class CompanionPublicationError(ValueError):
    """A durable Companion publication is incomplete or inconsistent."""


@dataclass(frozen=True)
class PublishedCompanion:
    publication: Publication
    publication_ref: ArtifactRef
    layer_refs: tuple[ArtifactRef, ...]
    fragment_refs: tuple[ArtifactRef, ...]
    resource_refs: tuple[ArtifactRef, ...]


def publish_companion(
    context: RunContext,
    *,
    source: RichDocument,
    title: str,
    authors: Sequence[str],
    source_language: str,
    target_language: str,
    translation_mode: str,
    reader_labels: Mapping[str, str],
    chapters: Sequence[Mapping[str, Any]],
    glossary: Sequence[Mapping[str, Any]],
    bibliography: Sequence[Mapping[str, Any]],
    paper_cache_root: str | Path | None = None,
) -> PublishedCompanion:
    """Publish immutable overlay revisions, their layers, and one publication."""

    blocks = {item.block_id: item for item in source.blocks}
    source_identity = Publication(source).source
    translation_revisions: list[FragmentRevision] = []
    companion_revisions: list[FragmentRevision] = []
    for chapter in chapters:
        chapter_id = _string(chapter, "chapter_id")
        raw_translation = chapter.get("translation_result")
        if translation_mode == "enabled":
            try:
                selection = load_translation_selection(
                    context,
                    _mapping(raw_translation, "translation result"),
                    source=source,
                    block_ids=_string_list(
                        chapter.get("block_ids"), "chapter block IDs"
                    ),
                    target_language=target_language,
                )
            except CompanionTranslationResultError as exc:
                raise CompanionPublicationError(str(exc)) from exc
            translation_revisions.extend(selection.revisions)
        elif raw_translation is not None:
            raise CompanionPublicationError(
                "a skipped translation must not contain a translation result"
            )
        for raw in _mapping_list(
            chapter.get("learning_units"), "learning units"
        ):
            anchors = _string_list(raw.get("anchor_block_ids"), "unit anchors")
            block = _block(blocks, anchors[0])
            purpose = _string(raw, "purpose")
            unit_id = _string(raw, "unit_id")
            title_value = _string(raw, "title")
            content = normalize_markdown(_string(raw, "content_markdown"))
            fragment_identity = json.dumps(
                {
                    "source": source_identity.rich_document_digest,
                    "chapter_id": chapter_id,
                    "unit_id": unit_id,
                    "anchors": anchors,
                    "purpose": purpose,
                    "title": title_value,
                    "content": content,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            companion_revisions.append(
                FragmentRevision(
                    source=source_identity,
                    fragment_id=_fragment_id(
                        "companion", fragment_identity
                    ),
                    revision=1,
                    parent_semantic_digest=None,
                    anchor=FragmentAnchor(
                        AnchorKind.BLOCK,
                        block.block_id,
                        tuple(
                            anchor_block_from_rich_block(_block(blocks, item))
                            for item in anchors
                        ),
                    ),
                    priority=101 if purpose in {"chapter", "section"} else 20,
                    role="guide" if purpose in {"chapter", "section"} else "companion",
                    language=target_language,
                    title=title_value,
                    citation_ids=tuple(
                        _string_list(raw.get("citations"), "unit citations")
                    ),
                    provenance={
                        "producer": "arc-companion",
                        "chapter_id": chapter_id,
                        "unit_id": unit_id,
                        "purpose": purpose,
                    },
                    markdown_body=content,
                )
            )

    fragment_artifacts: list[ArtifactRef] = []
    layers: list[tuple[str, Layer, ArtifactRef]] = []
    for producer, artifact_id, revisions in (
        ("arc-translate", TRANSLATION_LAYER_ARTIFACT, translation_revisions),
        ("arc-companion", COMPANION_LAYER_ARTIFACT, companion_revisions),
    ):
        if not revisions:
            continue
        revision_refs = []
        for revision in revisions:
            relative = fragment_revision_storage_path(revision)
            ref = context.artifacts.publish_bytes(
                f"publication/{relative}",
                encode_fragment_revision(revision).encode("utf-8"),
                media_type="text/markdown",
            )
            fragment_artifacts.append(ref)
            revision_refs.append(fragment_revision_ref(relative, revision))
        layer = Layer(source_identity, producer, tuple(revision_refs))
        layer_ref = context.artifacts.publish_json(
            artifact_id, layer_to_document(layer)
        )
        layers.append((artifact_id.removeprefix("publication/"), layer, layer_ref))

    paper = ArcPaperService(cache_root=paper_cache_root)
    resource_artifacts: list[ArtifactRef] = []
    resources: list[dict[str, Any]] = []
    for asset in source.assets:
        try:
            cached = paper.repository.get_asset(asset.artifact_digest)
            payload = paper.repository.read_asset_bytes(cached)
        except (OSError, SourceRepositoryError) as exc:
            raise CompanionPublicationError(
                "source asset is unavailable for the run-owned publication: "
                f"{asset.artifact_digest}"
            ) from exc
        relative = f"resources/{asset.artifact_digest}"
        resource_artifacts.append(
            context.artifacts.publish_bytes(
                f"publication/{relative}",
                payload,
                media_type=asset.media_type,
            )
        )
        resources.append(
            {
                "artifact_digest": asset.artifact_digest,
                "media_type": asset.media_type,
                "logical_name": asset.logical_name,
                "size": asset.size,
                "path": relative,
            }
        )

    publication = Publication(
        source_document=source,
        layers=tuple(
            layer.reference(relative)
            for relative, layer, _artifact in layers
        ),
        outline=_publication_outline(chapters, source),
        glossary=tuple(dict(item) for item in glossary),
        bibliography=tuple(dict(item) for item in bibliography),
        labels=dict(reader_labels),
        resources=tuple(resources),
        reader_profile={
            "title": title,
            "authors": list(authors),
            "source_language": source_language,
            "target_language": target_language,
            "translation_mode": translation_mode,
        },
    )
    publication_ref = context.artifacts.publish_json(
        PUBLICATION_ARTIFACT, publication_to_document(publication)
    )
    return PublishedCompanion(
        publication,
        publication_ref,
        tuple(item[2] for item in layers),
        tuple(fragment_artifacts),
        tuple(resource_artifacts),
    )


def _publication_outline(
    chapters: Sequence[Mapping[str, Any]],
    source: RichDocument,
) -> tuple[PublicationOutlineItem, ...]:
    """Build the reader outline from program-owned chapter boundaries."""

    block_indices = {
        block.block_id: index for index, block in enumerate(source.blocks)
    }
    values: list[PublicationOutlineItem] = []
    ordinal = 0
    for raw_chapter in chapters:
        chapter_id = _string(raw_chapter, "chapter_id")
        title = _string(raw_chapter, "title")
        block_ids = _string_list(
            raw_chapter.get("block_ids"), "chapter block IDs"
        )
        try:
            chapter_indices = [block_indices[item] for item in block_ids]
        except KeyError as exc:
            raise CompanionPublicationError(
                "chapter outline refers to an unknown source block"
            ) from exc
        if chapter_indices != list(
            range(chapter_indices[0], chapter_indices[-1] + 1)
        ):
            raise CompanionPublicationError(
                "chapter outline blocks must be contiguous and ordered"
            )
        anchor_id = _string(raw_chapter, "display_anchor_block_id")
        if anchor_id not in block_ids:
            raise CompanionPublicationError(
                "chapter display anchor is outside its source chapter"
            )
        chapter_start = chapter_indices[0]
        chapter_end = chapter_indices[-1] + 1
        values.append(
            PublicationOutlineItem(
                section_id=chapter_id,
                title=title,
                level=1,
                ordinal=ordinal,
                path=(chapter_id,),
                block_start=chapter_start,
                block_end=chapter_end,
                anchor_block_id=anchor_id,
            )
        )
        ordinal += 1

        section_ids = _string_list(
            raw_chapter.get("section_block_ids"),
            "chapter section block IDs",
        )
        section_titles = _string_list(
            raw_chapter.get("section_titles"), "chapter section titles"
        )
        section_levels = _integer_list(
            raw_chapter.get("section_levels"), "chapter section levels"
        )
        if not (
            len(section_ids) == len(section_titles) == len(section_levels)
        ):
            raise CompanionPublicationError(
                "chapter section outline fields have different lengths"
            )
        try:
            section_starts = [block_indices[item] for item in section_ids]
        except KeyError as exc:
            raise CompanionPublicationError(
                "section outline refers to an unknown source block"
            ) from exc
        if (
            section_starts != sorted(section_starts)
            or len(set(section_starts)) != len(section_starts)
            or any(
                item < chapter_start or item >= chapter_end
                for item in section_starts
            )
        ):
            raise CompanionPublicationError(
                "section outline is not ordered inside its chapter"
            )

        path_stack: list[tuple[int, str]] = [(1, chapter_id)]
        for index, (
            block_id,
            section_title,
            level,
            block_start,
        ) in enumerate(
            zip(
                section_ids,
                section_titles,
                section_levels,
                section_starts,
                strict=True,
            ),
            1,
        ):
            if level < 2:
                raise CompanionPublicationError(
                    "chapter subsection levels must be at least two"
                )
            while path_stack and path_stack[-1][0] >= level:
                path_stack.pop()
            section_id = f"{chapter_id}-section-{index:04d}"
            path = [item[1] for item in path_stack]
            path.append(section_id)
            block_end = chapter_end
            for later_start, later_level in zip(
                section_starts[index:],
                section_levels[index:],
                strict=True,
            ):
                if later_level <= level:
                    block_end = later_start
                    break
            values.append(
                PublicationOutlineItem(
                    section_id=section_id,
                    title=section_title,
                    level=level,
                    ordinal=ordinal,
                    path=tuple(path),
                    block_start=block_start,
                    block_end=block_end,
                    anchor_block_id=block_id,
                )
            )
            ordinal += 1
            path_stack.append((level, section_id))
    return tuple(values)


def build_result_document(published: PublishedCompanion) -> dict[str, Any]:
    return {
        "schema_version": BUILD_RESULT_SCHEMA,
        "publication": ref_document(published.publication_ref),
        "layers": [ref_document(item) for item in published.layer_refs],
        "fragments": [ref_document(item) for item in published.fragment_refs],
        "resources": [ref_document(item) for item in published.resource_refs],
    }


def load_published_companion(
    store: ImmutableArtifactStore, result_ref: ArtifactRef
) -> PublishedCompanion:
    try:
        result = json.loads(store.read_bytes(result_ref).decode("utf-8"))
        if not isinstance(result, Mapping) or set(result) != {
            "schema_version",
            "publication",
            "layers",
            "fragments",
            "resources",
        }:
            raise ValueError("build result has invalid fields")
        if result["schema_version"] != BUILD_RESULT_SCHEMA:
            raise ValueError("unsupported build result schema")
        publication_ref = decode_artifact_ref(
            _mapping(result["publication"], "publication reference")
        )
        layer_refs = tuple(
            decode_artifact_ref(item)
            for item in _mapping_list(result["layers"], "layer references")
        )
        fragment_refs = tuple(
            decode_artifact_ref(item)
            for item in _mapping_list(
                result["fragments"], "fragment references"
            )
        )
        resource_refs = tuple(
            decode_artifact_ref(item)
            for item in _mapping_list(
                result["resources"], "resource references"
            )
        )
        publication_value = json.loads(
            store.read_bytes(publication_ref).decode("utf-8")
        )
        publication = publication_from_document(publication_value)
        published = PublishedCompanion(
            publication,
            publication_ref,
            layer_refs,
            fragment_refs,
            resource_refs,
        )
        _validate_published_artifacts(store, published)
        return published
    except (
        ArcJobsError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CompanionPublicationError(
            "run publication artifacts are invalid"
        ) from exc


def materialize_published_companion(
    store: ImmutableArtifactStore,
    published: PublishedCompanion,
    workspace: str | Path,
) -> Path:
    """Materialize exact run-owned artifacts into an arc-render workspace."""

    root = Path(workspace).resolve()
    layers = _validate_published_artifacts(store, published)
    root.mkdir(parents=True, exist_ok=True)
    publication_path = root / "publication.json"
    _write_exact(publication_path, store.read_bytes(published.publication_ref))
    if len(published.publication.layers) != len(published.layer_refs):
        raise CompanionPublicationError(
            "publication layer artifacts do not match its layer references"
        )
    for layer_reference, ref in zip(
        published.publication.layers, published.layer_refs, strict=True
    ):
        _write_exact(root / layer_reference.path, store.read_bytes(ref))
    revision_paths = _revision_paths(layers)
    for ref in published.fragment_refs:
        payload = store.read_bytes(ref)
        revision = _fragment_from_bytes(payload)
        relative = revision_paths[_revision_identity(revision)][0]
        _write_exact(root / relative, payload)
    resource_paths = {
        _string(item, "artifact_digest"): _string(item, "path")
        for item in published.publication.resources
    }
    if len(resource_paths) != len(published.resource_refs):
        raise CompanionPublicationError(
            "publication resource artifacts do not match its resources"
        )
    for ref in published.resource_refs:
        try:
            relative = resource_paths[ref.digest.value]
        except KeyError as exc:
            raise CompanionPublicationError(
                "publication resource artifact has no declared resource"
            ) from exc
        _write_exact(root / relative, store.read_bytes(ref))
    return publication_path


def _validate_published_artifacts(
    store: ImmutableArtifactStore,
    published: PublishedCompanion,
) -> tuple[Layer, ...]:
    try:
        encoded_publication = json.loads(
            store.read_bytes(published.publication_ref).decode("utf-8")
        )
        if (
            publication_from_document(encoded_publication)
            != published.publication
        ):
            raise ValueError("publication artifact differs from its manifest")
        if len(published.publication.layers) != len(
            published.layer_refs
        ):
            raise ValueError(
                "publication layer artifacts do not match its references"
            )
        layers = []
        for expected, artifact in zip(
            published.publication.layers,
            published.layer_refs,
            strict=True,
        ):
            raw = json.loads(store.read_bytes(artifact).decode("utf-8"))
            layer = layer_from_document(raw)
            if layer.reference(expected.path) != expected:
                raise ValueError(
                    "layer artifact differs from its publication reference"
                )
            layers.append(layer)

        expected_revisions = _revision_paths(layers)
        actual_revisions = {}
        for artifact in published.fragment_refs:
            revision = _fragment_from_bytes(store.read_bytes(artifact))
            identity = _revision_identity(revision)
            if identity in actual_revisions:
                raise ValueError("publication repeats a fragment artifact")
            actual_revisions[identity] = revision
        if set(actual_revisions) != set(expected_revisions):
            raise ValueError(
                "publication fragment artifacts do not match its layers"
            )
        for identity, revision in actual_revisions.items():
            reference = expected_revisions[identity][1]
            if (
                revision.fragment_id != reference.fragment_id
                or revision.revision != reference.revision
                or revision.semantic_digest != reference.semantic_digest
                or revision.source != published.publication.source
            ):
                raise ValueError(
                    "fragment artifact differs from its layer reference"
                )

        resources = {
            _string(item, "artifact_digest"): item
            for item in published.publication.resources
        }
        actual_resources = {
            artifact.digest.value: artifact
            for artifact in published.resource_refs
        }
        if (
            len(actual_resources) != len(published.resource_refs)
            or set(actual_resources) != set(resources)
        ):
            raise ValueError(
                "publication resource artifacts do not match its resources"
            )
        for digest, artifact in actual_resources.items():
            resource = resources[digest]
            if (
                artifact.digest.size != resource.get("size")
                or artifact.media_type != resource.get("media_type")
            ):
                raise ValueError(
                    "resource artifact metadata differs from the publication"
                )
            store.read_bytes(artifact)
        return tuple(layers)
    except (
        ArcJobsError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise CompanionPublicationError(
            "run publication artifacts are invalid"
        ) from exc


def _revision_paths(
    layers: Sequence[Layer],
) -> dict[tuple[str, int, str], tuple[str, FragmentRevisionRef]]:
    result: dict[
        tuple[str, int, str], tuple[str, FragmentRevisionRef]
    ] = {}
    paths: set[str] = set()
    for layer in layers:
        for reference in layer.initial_revisions:
            identity = _revision_identity(reference)
            if identity in result:
                raise ValueError(
                    "publication layers repeat a fragment revision"
                )
            if reference.path in paths:
                raise ValueError("publication layers repeat a fragment path")
            result[identity] = (reference.path, reference)
            paths.add(reference.path)
    return result


def _revision_identity(
    value: FragmentRevision | FragmentRevisionRef,
) -> tuple[str, int, str]:
    return (
        value.fragment_id,
        value.revision,
        value.semantic_digest,
    )


def _fragment_from_bytes(payload: bytes) -> FragmentRevision:
    from arc_render import decode_fragment_revision

    return decode_fragment_revision(payload.decode("utf-8"))


def _write_exact(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != payload:
        raise CompanionPublicationError(
            f"publication workspace contains conflicting bytes: {path}"
        )
    atomic_write_bytes(path, payload)


def _fragment_id(role: str, identity: str) -> str:
    import hashlib

    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{role}-{digest}"


def _block(blocks: Mapping[str, Any], block_id: str) -> Any:
    try:
        return blocks[block_id]
    except KeyError as exc:
        raise CompanionPublicationError(
            f"overlay refers to unknown source block: {block_id}"
        ) from exc


def _mapping(value: Any, description: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompanionPublicationError(f"{description} must be an object")
    return value


def _mapping_list(value: Any, description: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise CompanionPublicationError(
            f"{description} must be an array of objects"
        )
    return list(value)


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CompanionPublicationError(f"{key} must be a non-empty string")
    return item


def _string_list(value: Any, description: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise CompanionPublicationError(
            f"{description} must contain non-empty strings"
        )
    return list(value)


def _integer_list(value: Any, description: str) -> list[int]:
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool)
        for item in value
    ):
        raise CompanionPublicationError(
            f"{description} must contain integers"
        )
    return list(value)


__all__ = [
    "BUILD_RESULT_SCHEMA",
    "COMPANION_LAYER_ARTIFACT",
    "PUBLICATION_ARTIFACT",
    "TRANSLATION_LAYER_ARTIFACT",
    "CompanionPublicationError",
    "PublishedCompanion",
    "build_result_document",
    "load_published_companion",
    "materialize_published_companion",
    "publish_companion",
]
