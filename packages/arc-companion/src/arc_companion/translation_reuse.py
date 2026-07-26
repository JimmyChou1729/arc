"""Exact, target-owned reuse of successful Companion translation artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from arc_jobs import (
    ArtifactRef,
    ImmutableArtifactStore,
    RunContext,
    RunError,
    RunRepository,
    RunStatus,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
)
from arc_paper import RichDocument, rich_document_from_document

from .contracts import AcceptedBook, CompanionContentCodec
from .project import CompanionProjectPaths
from .request_contracts import (
    CompanionBuildRequest,
    CompanionGenerationRecipe,
    decode_handler_semantic_input,
)
from .source_planning import plan_source_chapters


TRANSLATION_REUSE_BUNDLE_SCHEMA = "arc.companion.translation_reuse_bundle.v1"
TRANSLATION_REUSE_RECEIPT_SCHEMA = "arc.companion.translation_reuse_receipt.v1"
TRANSLATION_REUSE_RECEIPT_ARTIFACT = "translation-reuse/receipt"
_BUNDLE_CANDIDATE = "translation-reuse/bundle.json"
_OBJECTS_PREFIX = "translation-reuse/objects"
_LANGUAGE_ARTIFACT = "translation-v2/language/result"
_GLOSSARY_ARTIFACT = "translation-v2/glossary/result"


class TranslationReuseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TranslationReuseSource:
    """A successful selected Companion run in another (or the same) project."""

    project_dir: Path
    run_id: str | None = None

    def __init__(
        self, project_dir: str | Path, run_id: str | None = None
    ) -> None:
        path = Path(project_dir)
        if run_id is not None and (
            not isinstance(run_id, str) or not run_id.strip()
        ):
            raise ValueError("translation reuse run_id must be non-empty")
        object.__setattr__(self, "project_dir", path)
        object.__setattr__(self, "run_id", run_id.strip() if run_id else None)

    def resolve(self) -> tuple[CompanionProjectPaths, str]:
        project = CompanionProjectPaths.load(self.project_dir)
        run_id = self.run_id or project.current_run_id
        if run_id is None:
            raise TranslationReuseError(
                "translation_reuse_source_not_selected",
                "translation reuse source project has no selected run",
            )
        return project, run_id


@dataclass(frozen=True)
class TranslationReusePlan:
    """Verified source bytes and their semantic compatibility digest."""

    reuse_digest: str
    bundle: Mapping[str, Any]
    payloads: Mapping[str, bytes]


@dataclass(frozen=True)
class TranslationReuseReceipt:
    document: Mapping[str, Any]
    artifact_ref: ArtifactRef


@dataclass(frozen=True)
class _SourceArtifact:
    role: str
    source_artifact_id: str
    target_artifact_id: str
    media_type: str
    payload: bytes
    chapter_id: str | None = None
    block_ids: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def descriptor(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "chapter_id": self.chapter_id,
            "block_ids": list(self.block_ids),
            "source_artifact_id": self.source_artifact_id,
            "target_artifact_id": self.target_artifact_id,
            "media_type": self.media_type,
            "sha256": self.digest,
            "size_bytes": len(self.payload),
        }


def plan_translation_reuse(
    source: TranslationReuseSource,
    request: CompanionBuildRequest,
    recipe: CompanionGenerationRecipe,
) -> TranslationReusePlan:
    """Verify a successful source run and collect its exact final translations."""

    from arc_translate import BlocksResult, GlossaryResult, LanguageResult

    project, run_id = source.resolve()
    repository = RunRepository(project.jobs_root)
    snapshot = repository.inspect(run_id).snapshot
    if snapshot.status is not RunStatus.SUCCEEDED or snapshot.result_ref is None:
        raise TranslationReuseError(
            "translation_reuse_source_not_successful",
            "translation reuse source run must be successful",
        )
    spec = repository.read_spec(run_id)
    # Imported lazily to keep render-only package imports lightweight.
    from .build import COMPANION_BUILD_HANDLER

    if spec.handler != COMPANION_BUILD_HANDLER:
        raise TranslationReuseError(
            "translation_reuse_handler_incompatible",
            "translation reuse source uses an incompatible Companion handler",
        )
    try:
        source_request, source_recipe = decode_handler_semantic_input(
            spec.semantic_input
        )
    except (TypeError, ValueError) as exc:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "translation reuse source request is invalid",
        ) from exc
    if (
        source_request.source.document_digest
        != request.source.document_digest
        or source_request.source.source.artifact_digest
        != request.source.source.artifact_digest
    ):
        raise TranslationReuseError(
            "translation_reuse_source_mismatch",
            "translation reuse requires the same source document and source artifact",
        )
    if source_request.target_language != request.target_language:
        raise TranslationReuseError(
            "translation_reuse_target_language_mismatch",
            "translation reuse target language differs from the requested language",
        )
    if source_recipe.approx_term_count != recipe.approx_term_count:
        raise TranslationReuseError(
            "translation_reuse_glossary_size_mismatch",
            "translation reuse glossary size differs from the requested size",
        )

    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    effective = _effective_source(
        store, source_request, recovery_epoch=snapshot.recovery_epoch
    )
    book, book_ref = _accepted_book(store, snapshot.result_ref)
    if (
        book.document_digest != effective.document_digest
        or book.target_language != request.target_language
    ):
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source accepted book is not bound to its effective source",
        )
    chapters = plan_source_chapters(effective)
    expected_chapters = [item.chapter_id for item in chapters]
    if [item.chapter_id for item in book.chapters] != expected_chapters:
        raise TranslationReuseError(
            "translation_reuse_chapters_mismatch",
            "source accepted book chapters do not match deterministic source chapters",
        )

    language_artifact = _read_artifact(
        store, snapshot.recovery_epoch, _LANGUAGE_ARTIFACT, role="language"
    )
    try:
        language = LanguageResult.from_document(
            _json_payload(language_artifact.payload, "language result")
        )
    except (TypeError, ValueError) as exc:
        raise TranslationReuseError(
            "translation_reuse_language_invalid",
            "source language result is invalid",
        ) from exc
    _require_result_binding(
        language.document_digest,
        language.source_digest,
        effective,
        description="language result",
    )
    if (
        language.target_language != request.target_language
        or language.mode != book.translation_mode
        or language.language_tag != book.source_language
    ):
        raise TranslationReuseError(
            "translation_reuse_language_mismatch",
            "source language result does not match the accepted book",
        )

    artifacts = [language_artifact]
    glossary_digest: str | None = None
    if language.mode == "enabled":
        glossary_artifact = _read_artifact(
            store, snapshot.recovery_epoch, _GLOSSARY_ARTIFACT, role="glossary"
        )
        try:
            glossary = GlossaryResult.from_document(
                _json_payload(glossary_artifact.payload, "glossary result")
            )
        except (TypeError, ValueError) as exc:
            raise TranslationReuseError(
                "translation_reuse_glossary_invalid",
                "source glossary result is invalid",
            ) from exc
        _require_result_binding(
            glossary.document_digest,
            glossary.source_digest,
            effective,
            description="glossary result",
        )
        if (
            glossary.target_language != request.target_language
            or glossary.approx_count != recipe.approx_term_count
        ):
            raise TranslationReuseError(
                "translation_reuse_glossary_mismatch",
                "source glossary result is incompatible with the target build",
            )
        glossary_digest = glossary_artifact.digest
        artifacts.append(glossary_artifact)

        accepted_by_chapter = {
            chapter.chapter_id: chapter for chapter in book.chapters
        }
        for chapter in chapters:
            logical_id = f"chapters/{chapter.chapter_id}/translation/result"
            artifact = _read_artifact(
                store,
                snapshot.recovery_epoch,
                logical_id,
                role="translation",
                chapter_id=chapter.chapter_id,
                block_ids=chapter.block_ids,
            )
            try:
                result = BlocksResult.from_document(
                    _json_payload(artifact.payload, "translation result")
                )
            except (TypeError, ValueError) as exc:
                raise TranslationReuseError(
                    "translation_reuse_translation_invalid",
                    f"source translation result is invalid for {chapter.chapter_id}",
                ) from exc
            _require_result_binding(
                result.document_digest,
                result.source_digest,
                effective,
                description="translation result",
            )
            if (
                result.source_language != language.language_tag
                or result.target_language != request.target_language
                or result.mode != "enabled"
                or [item["block_id"] for item in result.translations]
                != list(chapter.block_ids)
            ):
                raise TranslationReuseError(
                    "translation_reuse_translation_mismatch",
                    f"source translation coverage differs for {chapter.chapter_id}",
                )
            accepted = accepted_by_chapter[chapter.chapter_id]
            if [
                {"block_id": item.block_id, "text": item.text}
                for item in accepted.translations
            ] != [dict(item) for item in result.translations]:
                raise TranslationReuseError(
                    "translation_reuse_accepted_book_mismatch",
                    f"accepted book translations differ for {chapter.chapter_id}",
                )
            artifacts.append(artifact)
    elif any(chapter.translations for chapter in book.chapters) or book.glossary:
        raise TranslationReuseError(
            "translation_reuse_accepted_book_mismatch",
            "translation-skipped source book contains translated content",
        )

    binding = {
        "schema_version": "arc.companion.translation_reuse_binding.v1",
        "effective_document_digest": effective.document_digest,
        "source_artifact_digest": effective.source.artifact_digest,
        "target_language": request.target_language,
        "approx_term_count": recipe.approx_term_count,
        "mode": language.mode,
        "language_result_digest": language_artifact.digest,
        "glossary_result_digest": glossary_digest,
        "chapters": [
            {
                "chapter_id": item.chapter_id,
                "block_ids": list(item.block_ids),
                "translation_result_digest": next(
                    (
                        artifact.digest
                        for artifact in artifacts
                        if artifact.chapter_id == item.chapter_id
                    ),
                    None,
                ),
            }
            for item in chapters
        ],
    }
    content_artifacts = [item.descriptor() for item in artifacts]
    reuse_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "binding": binding,
                "artifacts": [
                    {
                        key: value
                        for key, value in item.items()
                        if key != "source_artifact_id"
                    }
                    for item in content_artifacts
                ],
            }
        )
    ).hexdigest()
    source_spec_digest = hashlib.sha256(
        canonical_json_bytes(
            {"handler": spec.handler, "semantic_input": dict(spec.semantic_input)}
        )
    ).hexdigest()
    bundle = {
        "schema_version": TRANSLATION_REUSE_BUNDLE_SCHEMA,
        "reuse_digest": reuse_digest,
        "source": {
            "run_id": run_id,
            "spec_sha256": source_spec_digest,
            "result_artifact_id": snapshot.result_ref.artifact_id,
            "result_sha256": snapshot.result_ref.digest.value,
            "accepted_book_artifact_id": book_ref.artifact_id,
            "accepted_book_sha256": book_ref.digest.value,
        },
        "binding": binding,
        "artifacts": content_artifacts,
    }
    return TranslationReusePlan(
        reuse_digest,
        bundle,
        {item.digest: item.payload for item in artifacts},
    )


def stage_translation_reuse_plan(
    repository: RunRepository,
    run_id: str,
    plan: TranslationReusePlan,
) -> None:
    """Atomically commit a verified bundle descriptor after target-owned copies."""

    spec = repository.read_spec(run_id)
    request, _recipe = decode_handler_semantic_input(spec.semantic_input)
    if request.translation_reuse_digest != plan.reuse_digest:
        raise TranslationReuseError(
            "translation_reuse_digest_mismatch",
            "target run semantic input does not match the staged reuse bundle",
        )
    working = repository.working_state(run_id)
    working.materialize(spec)
    for digest, payload in plan.payloads.items():
        if hashlib.sha256(payload).hexdigest() != digest:
            raise TranslationReuseError(
                "translation_reuse_payload_mismatch",
                "translation reuse payload changed before staging",
            )
        atomic_write_bytes(
            working.candidate_path(f"{_OBJECTS_PREFIX}/{digest}"), payload
        )
    atomic_write_json(working.candidate_path(_BUNDLE_CANDIDATE), dict(plan.bundle))


class StagedTranslationReuseAdapter:
    """Consume one staged exact-reuse bundle through Companion's adapter protocol."""

    def __init__(
        self,
        expected_digest: str,
        *,
        approx_term_count: int,
    ) -> None:
        self.expected_digest = expected_digest
        self.approx_term_count = approx_term_count
        self._context_key: tuple[str, int] | None = None
        self._documents: dict[str, dict[str, Any]] = {}

    def detect_language(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        target_language: str,
        **_kwargs: Any,
    ) -> Mapping[str, Any] | RunError:
        error = self._install(context, source, target_language)
        if error is not None:
            return error
        return dict(self._documents[_LANGUAGE_ARTIFACT])

    def build_glossary(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        target_language: str,
        approx_count: int,
        **_kwargs: Any,
    ) -> Mapping[str, Any] | RunError:
        error = self._install(context, source, target_language)
        if error is not None:
            return error
        if approx_count != self.approx_term_count:
            return RunError(
                "translation_reuse_glossary_size_mismatch",
                "staged glossary size differs from the requested size",
            )
        value = self._documents.get(_GLOSSARY_ARTIFACT)
        if value is None:
            return RunError(
                "translation_reuse_glossary_missing",
                "staged translation reuse has no glossary result",
            )
        return dict(value)

    def translate_blocks(
        self,
        context: RunContext,
        source: RichDocument,
        *,
        block_ids: Sequence[str],
        target_language: str,
        artifact_prefix: str,
        **_kwargs: Any,
    ) -> Mapping[str, Any] | RunError:
        error = self._install(context, source, target_language)
        if error is not None:
            return error
        artifact_id = f"{artifact_prefix}/result"
        value = self._documents.get(artifact_id)
        if value is None:
            return RunError(
                "translation_reuse_translation_missing",
                f"staged translation reuse has no result for {artifact_prefix}",
            )
        if [item.get("block_id") for item in value.get("translations", [])] != list(
            block_ids
        ):
            return RunError(
                "translation_reuse_translation_mismatch",
                f"staged translation coverage differs for {artifact_prefix}",
            )
        return dict(value)

    def _install(
        self, context: RunContext, source: RichDocument, target_language: str
    ) -> RunError | None:
        key = (context.run_id, context.recovery_epoch)
        if self._context_key == key:
            return None
        try:
            bundle = context.working.read_candidate_json(_BUNDLE_CANDIDATE)
            _validate_bundle(bundle, expected_digest=self.expected_digest)
            binding = bundle["binding"]
            if (
                binding["effective_document_digest"] != source.document_digest
                or binding["source_artifact_digest"]
                != source.source.artifact_digest
            ):
                raise TranslationReuseError(
                    "translation_reuse_effective_source_mismatch",
                    "staged translation reuse does not match the effective source",
                )
            if binding["target_language"] != target_language:
                raise TranslationReuseError(
                    "translation_reuse_target_language_mismatch",
                    "staged translation reuse target language differs",
                )
            documents: dict[str, dict[str, Any]] = {}
            payloads: list[tuple[Mapping[str, Any], bytes]] = []
            for descriptor in bundle["artifacts"]:
                digest = descriptor["sha256"]
                path = context.working.candidate_path(
                    f"{_OBJECTS_PREFIX}/{digest}"
                )
                payload = path.read_bytes()
                if (
                    hashlib.sha256(payload).hexdigest() != digest
                    or len(payload) != descriptor["size_bytes"]
                ):
                    raise TranslationReuseError(
                        "translation_reuse_payload_mismatch",
                        f"staged translation payload is corrupt: {path}",
                    )
                document = _json_payload(payload, "staged translation result")
                documents[descriptor["target_artifact_id"]] = document
                payloads.append((descriptor, payload))

            # Validate every target ID before publishing any missing artifact.
            for descriptor, payload in payloads:
                existing = context.artifacts.find(
                    descriptor["target_artifact_id"]
                )
                if (
                    existing is not None
                    and context.artifacts.read_bytes(existing) != payload
                ):
                    raise TranslationReuseError(
                        "translation_reuse_target_conflict",
                        "target run already contains different translation content "
                        f"for {descriptor['target_artifact_id']}",
                    )
            for descriptor, payload in payloads:
                context.artifacts.publish_bytes(
                    descriptor["target_artifact_id"],
                    payload,
                    media_type=descriptor["media_type"],
                )
            receipt = {
                "schema_version": TRANSLATION_REUSE_RECEIPT_SCHEMA,
                "reuse_digest": bundle["reuse_digest"],
                "source": bundle["source"],
                "target_run_id": context.run_id,
                "binding": bundle["binding"],
                "artifacts": bundle["artifacts"],
            }
            context.artifacts.publish_json(
                TRANSLATION_REUSE_RECEIPT_ARTIFACT, receipt
            )
            self._documents = documents
            self._context_key = key
            return None
        except TranslationReuseError as exc:
            return RunError(exc.code, str(exc))
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return RunError(
                "translation_reuse_bundle_invalid",
                f"staged translation reuse is invalid: {exc}",
            )


def read_translation_reuse_receipt(
    repository: RunRepository, run_id: str
) -> TranslationReuseReceipt | None:
    snapshot = repository.inspect(run_id).snapshot
    store = ImmutableArtifactStore(
        repository.run_directory(run_id), repository_root=repository.root
    )
    artifact_id = _physical_artifact_id(
        snapshot.recovery_epoch, TRANSLATION_REUSE_RECEIPT_ARTIFACT
    )
    ref = store.find(artifact_id)
    if ref is None:
        return None
    document = _json_payload(store.read_bytes(ref), "translation reuse receipt")
    if (
        set(document)
        != {
            "schema_version",
            "reuse_digest",
            "source",
            "target_run_id",
            "binding",
            "artifacts",
        }
        or document["schema_version"] != TRANSLATION_REUSE_RECEIPT_SCHEMA
        or document["target_run_id"] != run_id
    ):
        raise TranslationReuseError(
            "translation_reuse_receipt_invalid",
            "translation reuse receipt is invalid",
        )
    _validate_bundle(
        {
            "schema_version": TRANSLATION_REUSE_BUNDLE_SCHEMA,
            "reuse_digest": document["reuse_digest"],
            "source": document["source"],
            "binding": document["binding"],
            "artifacts": document["artifacts"],
        },
        expected_digest=str(document["reuse_digest"]),
    )
    return TranslationReuseReceipt(document, ref)


def _effective_source(
    store: ImmutableArtifactStore,
    request: CompanionBuildRequest,
    *,
    recovery_epoch: int,
) -> RichDocument:
    diagnostics_ref = store.find(
        _physical_artifact_id(recovery_epoch, "diagnostics/build")
    )
    if diagnostics_ref is None:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source run has no build diagnostics",
        )
    diagnostics = _json_payload(
        store.read_bytes(diagnostics_ref), "source build diagnostics"
    )
    if diagnostics.get("source_document_digest") != request.source.document_digest:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source build diagnostics do not match the source request",
        )
    if diagnostics.get("status") != "applied":
        if (
            diagnostics.get("effective_document_digest")
            != request.source.document_digest
        ):
            raise TranslationReuseError(
                "translation_reuse_source_invalid",
                "source build diagnostics contain an invalid effective digest",
            )
        return request.source
    effective_ref = store.find(
        _physical_artifact_id(recovery_epoch, "source/effective")
    )
    if effective_ref is None:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source run has no effective source artifact",
        )
    try:
        effective = rich_document_from_document(
            _json_payload(store.read_bytes(effective_ref), "effective source")
        )
    except (TypeError, ValueError) as exc:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source effective document is invalid",
        ) from exc
    if diagnostics.get("effective_document_digest") != effective.document_digest:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source effective document does not match build diagnostics",
        )
    return effective


def _accepted_book(
    store: ImmutableArtifactStore, result_ref: ArtifactRef
) -> tuple[AcceptedBook, ArtifactRef]:
    from arc_jobs import decode_artifact_ref

    try:
        result = _json_payload(store.read_bytes(result_ref), "Companion result")
        if set(result) != {"schema_version", "accepted_book"} or (
            result["schema_version"] != "arc.companion.build_result.v1"
        ):
            raise ValueError("unsupported Companion result")
        raw_ref = result["accepted_book"]
        if not isinstance(raw_ref, Mapping):
            raise ValueError("accepted book reference must be an object")
        book_ref = decode_artifact_ref(raw_ref)
        return CompanionContentCodec.loads(store.read_bytes(book_ref)), book_ref
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise TranslationReuseError(
            "translation_reuse_source_invalid",
            "source accepted book is invalid",
        ) from exc


def _read_artifact(
    store: ImmutableArtifactStore,
    recovery_epoch: int,
    logical_id: str,
    *,
    role: str,
    chapter_id: str | None = None,
    block_ids: Sequence[str] = (),
) -> _SourceArtifact:
    artifact_id = _physical_artifact_id(recovery_epoch, logical_id)
    ref = store.find(artifact_id)
    if ref is None:
        raise TranslationReuseError(
            "translation_reuse_artifact_missing",
            f"source run is missing {logical_id}",
        )
    return _SourceArtifact(
        role=role,
        source_artifact_id=ref.artifact_id,
        target_artifact_id=logical_id,
        media_type=ref.media_type,
        payload=store.read_bytes(ref),
        chapter_id=chapter_id,
        block_ids=tuple(block_ids),
    )


def _physical_artifact_id(recovery_epoch: int, logical_id: str) -> str:
    return (
        logical_id
        if recovery_epoch == 0
        else f"recovery-{recovery_epoch}/{logical_id}"
    )


def _require_result_binding(
    document_digest: str,
    source_digest: str,
    source: RichDocument,
    *,
    description: str,
) -> None:
    if (
        document_digest != source.document_digest
        or source_digest != source.source.artifact_digest
    ):
        raise TranslationReuseError(
            "translation_reuse_source_mismatch",
            f"source {description} does not match the effective source",
        )


def _json_payload(payload: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{description} is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    return value


def _validate_bundle(
    bundle: Mapping[str, Any], *, expected_digest: str
) -> None:
    if (
        set(bundle)
        != {"schema_version", "reuse_digest", "source", "binding", "artifacts"}
        or bundle.get("schema_version") != TRANSLATION_REUSE_BUNDLE_SCHEMA
        or bundle.get("reuse_digest") != expected_digest
        or not isinstance(bundle.get("source"), Mapping)
        or not isinstance(bundle.get("binding"), Mapping)
        or not isinstance(bundle.get("artifacts"), list)
    ):
        raise TranslationReuseError(
            "translation_reuse_bundle_invalid",
            "staged translation reuse bundle has invalid fields",
        )
    artifacts = bundle["artifacts"]
    if not artifacts:
        raise TranslationReuseError(
            "translation_reuse_bundle_invalid",
            "staged translation reuse bundle has no artifacts",
        )
    required = {
        "role",
        "chapter_id",
        "block_ids",
        "source_artifact_id",
        "target_artifact_id",
        "media_type",
        "sha256",
        "size_bytes",
    }
    if any(
        not isinstance(item, Mapping)
        or set(item) != required
        or not isinstance(item.get("sha256"), str)
        or len(item["sha256"]) != 64
        or not isinstance(item.get("size_bytes"), int)
        or isinstance(item["size_bytes"], bool)
        or item["size_bytes"] < 0
        for item in artifacts
    ):
        raise TranslationReuseError(
            "translation_reuse_bundle_invalid",
            "staged translation reuse artifact descriptors are invalid",
        )
    material = {
        "binding": dict(bundle["binding"]),
        "artifacts": [
            {
                key: value
                for key, value in dict(item).items()
                if key != "source_artifact_id"
            }
            for item in artifacts
        ],
    }
    actual = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    if actual != expected_digest:
        raise TranslationReuseError(
            "translation_reuse_digest_mismatch",
            "staged translation reuse content digest does not match",
        )


__all__ = [
    "TRANSLATION_REUSE_BUNDLE_SCHEMA",
    "TRANSLATION_REUSE_RECEIPT_ARTIFACT",
    "TRANSLATION_REUSE_RECEIPT_SCHEMA",
    "StagedTranslationReuseAdapter",
    "TranslationReuseError",
    "TranslationReusePlan",
    "TranslationReuseReceipt",
    "TranslationReuseSource",
    "plan_translation_reuse",
    "read_translation_reuse_receipt",
    "stage_translation_reuse_plan",
]
