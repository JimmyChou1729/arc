"""Reusable translation tasks that run inside an existing ``RunContext``."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias

from arc_jobs import (
    ArtifactRef,
    Awaiting,
    Failed,
    JsonValue,
    Paused,
    ResumeReason,
    RunContext,
    RunError,
    StoppedError,
    canonical_json_bytes,
    decode_artifact_ref,
    encode_artifact_ref,
)
from arc_llm import (
    JsonOutput,
    LLMCompleted,
    LLMExecutionOptions,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMStopped,
    LLMTaskService,
    ModelSelection,
    RESUME_SCHEMA_VERSION,
    ResumeInput,
    decode_resume_input,
    resume_input_matches,
)
from arc_paper import RichBlockKind, rich_block_to_document
from arc_render import (
    AnchorKind,
    FragmentAnchor,
    FragmentRevision,
    FragmentRevisionRef,
    Layer,
    anchor_block_from_rich_block,
    block_text_to_markdown,
    decode_fragment_revision,
    encode_fragment_revision,
    fragment_revision_storage_path,
    fragment_revision_ref,
    fragment_revision_ref_from_document,
    fragment_revision_ref_to_document,
    layer_from_document,
    layer_to_document,
    source_identity_from_rich_document,
)

from .contracts import (
    GLOSSARY_RESULT_SCHEMA,
    LANGUAGE_RESULT_SCHEMA,
    TRANSLATION_RESULT_SCHEMA,
    TranslationSource,
)
from .prompts import (
    GLOSSARY_PROMPT_VERSION,
    GLOSSARY_SCHEMA,
    LANGUAGE_PROMPT_VERSION,
    LANGUAGE_SCHEMA,
    REVIEW_PROMPT_VERSION,
    REVIEW_SCHEMA,
    TRANSLATION_PROMPT_VERSION,
    TRANSLATION_SCHEMA,
    glossary_prompt,
    language_prompt,
    review_prompt,
    translation_prompt,
)
from .source import (
    STRUCTURAL_FIGURE_PLACEHOLDER,
    TranslationSourceError,
    block_text,
    deterministic_language_samples,
    prompt_block,
    same_primary_language,
    source_blocks,
    validate_translation_text,
)


REVIEW_SUPERVISION_SCHEMA = "arc.translate.review_supervision.v1"
OUTPUT_SUPERVISION_SCHEMA = "arc.translate.output_supervision.v1"


class KeywordProvider(Protocol):
    def extract_keywords(
        self,
        context: RunContext,
        source: Any,
        *,
        structure: Any | None = None,
        section_ids: Sequence[str] | None = None,
        approx_count: int = 50,
        model: ModelSelection = ModelSelection(tier="medium"),
        resume_input: Mapping[str, JsonValue] | None = None,
        options: LLMExecutionOptions = LLMExecutionOptions(),
    ) -> Any: ...


@dataclass(frozen=True)
class LanguageResult:
    document_digest: str
    source_digest: str
    language_tag: str
    classification: str
    confidence: float
    target_language: str
    mode: str
    schema_version: str = LANGUAGE_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != LANGUAGE_RESULT_SCHEMA:
            raise ValueError("unsupported language result schema")
        if self.classification not in {"known", "mixed", "unknown"}:
            raise ValueError("language classification is invalid")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("language confidence is invalid")
        if self.mode not in {"enabled", "skipped"}:
            raise ValueError("translation mode is invalid")
        if any(
            not isinstance(item, str) or not item.strip()
            for item in (
                self.document_digest,
                self.source_digest,
                self.language_tag,
                self.target_language,
            )
        ):
            raise ValueError("language result contains an empty identity")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "document_digest": self.document_digest,
            "source_digest": self.source_digest,
            "language_tag": self.language_tag,
            "classification": self.classification,
            "confidence": float(self.confidence),
            "target_language": self.target_language,
            "mode": self.mode,
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "LanguageResult":
        _require_fields(
            value,
            {
                "schema_version",
                "document_digest",
                "source_digest",
                "language_tag",
                "classification",
                "confidence",
                "target_language",
                "mode",
            },
            "language result",
        )
        confidence = value["confidence"]
        if not isinstance(confidence, (int, float)) or isinstance(
            confidence, bool
        ):
            raise ValueError("language confidence must be a number")
        return cls(
            document_digest=_string(value, "document_digest"),
            source_digest=_string(value, "source_digest"),
            language_tag=_string(value, "language_tag"),
            classification=_string(value, "classification"),
            confidence=float(confidence),
            target_language=_string(value, "target_language"),
            mode=_string(value, "mode"),
            schema_version=_string(value, "schema_version"),
        )


@dataclass(frozen=True)
class GlossaryResult:
    document_digest: str
    source_digest: str
    target_language: str
    approx_count: int
    inventory_digest: str
    entries: tuple[Mapping[str, JsonValue], ...]
    schema_version: str = GLOSSARY_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != GLOSSARY_RESULT_SCHEMA:
            raise ValueError("unsupported glossary result schema")
        if (
            isinstance(self.approx_count, bool)
            or not isinstance(self.approx_count, int)
            or not 1 <= self.approx_count <= 200
        ):
            raise ValueError("glossary approx_count is invalid")
        if any(
            not isinstance(item, str) or not item
            for item in (
                self.document_digest,
                self.source_digest,
                self.target_language,
                self.inventory_digest,
            )
        ) or not _is_sha256(self.inventory_digest):
            raise ValueError("glossary identity is invalid")
        entries = tuple(dict(item) for item in self.entries)
        _validate_glossary_entries(entries)
        object.__setattr__(self, "entries", entries)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "document_digest": self.document_digest,
            "source_digest": self.source_digest,
            "target_language": self.target_language,
            "approx_count": self.approx_count,
            "inventory_digest": self.inventory_digest,
            "entries": [dict(item) for item in self.entries],
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "GlossaryResult":
        _require_fields(
            value,
            {
                "schema_version",
                "document_digest",
                "source_digest",
                "target_language",
                "approx_count",
                "inventory_digest",
                "entries",
            },
            "glossary result",
        )
        entries = _mapping_list(value["entries"], "glossary entries")
        return cls(
            document_digest=_string(value, "document_digest"),
            source_digest=_string(value, "source_digest"),
            target_language=_string(value, "target_language"),
            approx_count=_integer(value, "approx_count"),
            inventory_digest=_string(value, "inventory_digest"),
            entries=tuple(entries),
            schema_version=_string(value, "schema_version"),
        )


@dataclass(frozen=True)
class TranslationRevisionArtifact:
    revision: FragmentRevisionRef
    artifact: ArtifactRef

    def __post_init__(self) -> None:
        if not isinstance(self.revision, FragmentRevisionRef):
            raise ValueError("revision must be a FragmentRevisionRef")
        if not isinstance(self.artifact, ArtifactRef):
            raise ValueError("artifact must be an ArtifactRef")
        if self.artifact.media_type != "text/markdown":
            raise ValueError("translation revision artifact must be Markdown")

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "revision": fragment_revision_ref_to_document(self.revision),
            "artifact": encode_artifact_ref(self.artifact),
        }

    @classmethod
    def from_document(
        cls, value: Mapping[str, Any]
    ) -> "TranslationRevisionArtifact":
        _require_fields(
            value, {"revision", "artifact"}, "translation revision artifact"
        )
        return cls(
            fragment_revision_ref_from_document(value["revision"]),
            decode_artifact_ref(value["artifact"]),
        )


@dataclass(frozen=True)
class TranslationResult:
    source_language: str
    target_language: str
    mode: str
    coverage: str
    layer: Layer
    revision_artifacts: tuple[TranslationRevisionArtifact, ...]
    schema_version: str = TRANSLATION_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != TRANSLATION_RESULT_SCHEMA:
            raise ValueError("unsupported translation result schema")
        if self.mode not in {"enabled", "skipped"}:
            raise ValueError("translation result mode is invalid")
        if self.coverage not in {"document", "selection"}:
            raise ValueError("translation result coverage is invalid")
        if not isinstance(self.source_language, str) or not self.source_language:
            raise ValueError("source_language must be non-empty")
        if not isinstance(self.target_language, str) or not self.target_language:
            raise ValueError("target_language must be non-empty")
        if not isinstance(self.layer, Layer):
            raise ValueError("layer must be an arc-render Layer")
        artifacts = tuple(self.revision_artifacts)
        if any(
            not isinstance(item, TranslationRevisionArtifact)
            for item in artifacts
        ):
            raise ValueError("revision_artifacts contains an invalid item")
        refs = tuple(item.revision for item in artifacts)
        if refs != self.layer.initial_revisions:
            raise ValueError(
                "revision artifacts must exactly match the ordered layer"
            )
        if self.mode == "skipped" and artifacts:
            raise ValueError("skipped translation result must have no revisions")
        object.__setattr__(self, "revision_artifacts", artifacts)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "mode": self.mode,
            "coverage": self.coverage,
            "layer": layer_to_document(self.layer),
            "revision_artifacts": [
                item.to_document() for item in self.revision_artifacts
            ],
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "TranslationResult":
        _require_fields(
            value,
            {
                "schema_version",
                "source_language",
                "target_language",
                "mode",
                "coverage",
                "layer",
                "revision_artifacts",
            },
            "translation result",
        )
        artifacts = _mapping_list(
            value["revision_artifacts"], "translation revision artifacts"
        )
        return cls(
            source_language=_string(value, "source_language"),
            target_language=_string(value, "target_language"),
            mode=_string(value, "mode"),
            coverage=_string(value, "coverage"),
            layer=layer_from_document(value["layer"]),
            revision_artifacts=tuple(
                TranslationRevisionArtifact.from_document(item)
                for item in artifacts
            ),
            schema_version=_string(value, "schema_version"),
        )


WorkflowResult: TypeAlias = (
    LanguageResult | GlossaryResult | TranslationResult | Paused | RunError
)


class TranslationWorkflowService:
    """Run replay-safe language, glossary, and translation tasks in a parent run."""

    def __init__(
        self,
        task_service: Any | None = None,
        keyword_provider: KeywordProvider | None = None,
    ) -> None:
        self.task_service = task_service or LLMTaskService()
        self.keyword_provider = keyword_provider

    def detect_language(
        self,
        context: RunContext,
        source: TranslationSource,
        *,
        target_language: str,
        model: ModelSelection = ModelSelection(),
        execution: LLMExecutionOptions = LLMExecutionOptions(),
        resume_input: ResumeInput | None = None,
        artifact_prefix: str = "language",
    ) -> LanguageResult | Paused | RunError:
        artifact_id = f"{artifact_prefix}/result"
        existing = context.artifacts.find(artifact_id)
        if existing is not None:
            return LanguageResult.from_document(
                _read_json_artifact(context, existing, "language result")
            )
        request = LLMRequest(
            _task_id(
                "language",
                {
                    "document_digest": source.document_digest,
                    "target_language": target_language,
                    "prompt_contract": LANGUAGE_PROMPT_VERSION,
                },
            ),
            language_prompt(deterministic_language_samples(source)),
            JsonOutput(LANGUAGE_SCHEMA, repair="format"),
            model,
        )
        generated = _validated_generation(
            self.task_service,
            context,
            request,
            validator=_validate_language_output,
            candidate_id=f"language/{artifact_prefix}/result.json",
            resume_input=resume_input,
            options=execution,
            stopped_message="language detection stopped",
        )
        if isinstance(generated, (Paused, RunError)):
            return generated
        if isinstance(generated, _InvalidGeneratedOutput):
            return _output_supervision(
                context,
                artifact_prefix=artifact_prefix,
                stage="language",
                error=generated.error,
                candidate_path=generated.candidate_path,
            )
        value = generated
        classification = str(value["classification"])
        language_tag = str(value["language_tag"])
        mode = (
            "skipped"
            if classification == "known"
            and same_primary_language(language_tag, target_language)
            else "enabled"
        )
        result = LanguageResult(
            document_digest=source.document_digest,
            source_digest=source.source_digest,
            language_tag=language_tag,
            classification=classification,
            confidence=float(value["confidence"]),
            target_language=target_language,
            mode=mode,
        )
        context.artifacts.publish_json(artifact_id, result.to_document())
        return result

    def build_glossary(
        self,
        context: RunContext,
        source: TranslationSource,
        *,
        language: LanguageResult,
        target_language: str,
        keyword_structure: Any | None = None,
        keyword_section_ids: Sequence[str] | None = None,
        approx_count: int = 50,
        model: ModelSelection = ModelSelection(),
        execution: LLMExecutionOptions = LLMExecutionOptions(),
        resume_input: ResumeInput | None = None,
        term_input_budget_bytes: int = 32_000,
        artifact_prefix: str = "glossary",
    ) -> GlossaryResult | Paused | RunError:
        _validate_language_binding(language, source, target_language)
        artifact_id = f"{artifact_prefix}/result"
        existing = context.artifacts.find(artifact_id)
        if existing is not None:
            return GlossaryResult.from_document(
                _read_json_artifact(context, existing, "glossary result")
            )
        inventory_id = f"{artifact_prefix}/keyword-inventory"
        inventory_ref = context.artifacts.find(inventory_id)
        if inventory_ref is None:
            if self.keyword_provider is None:
                return RunError(
                    "keyword_provider_missing",
                    "glossary generation requires an arc-paper keyword provider",
                )
            try:
                keyword_options: dict[str, Any] = {}
                if keyword_structure is not None:
                    keyword_options["structure"] = keyword_structure
                    keyword_options["section_ids"] = keyword_section_ids
                keyword_outcome = self.keyword_provider.extract_keywords(
                    context,
                    source.rich,
                    approx_count=approx_count,
                    model=model,
                    resume_input=(
                        context.resume_input
                        if context.resume_input is not None
                        else None
                    ),
                    options=execution,
                    **keyword_options,
                )
                if isinstance(keyword_outcome, Paused):
                    return keyword_outcome
                if isinstance(keyword_outcome, Failed):
                    return keyword_outcome.error
                raw_inventory = keyword_outcome
                inventory = _keyword_inventory_document(raw_inventory)
                _validate_keyword_inventory(
                    inventory, source=source, approx_count=approx_count
                )
            except StoppedError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", "keyword_extraction_failed")
                return RunError(str(code), str(exc))
            inventory_ref = context.artifacts.publish_json(
                inventory_id, inventory
            )
        else:
            inventory = _read_json_artifact(
                context, inventory_ref, "keyword inventory"
            )
            _validate_keyword_inventory(
                inventory, source=source, approx_count=approx_count
            )
        terms = _mapping_list(inventory["terms"], "keyword terms")
        try:
            windows = _glossary_windows(
                terms,
                target_language=target_language,
                budget_bytes=term_input_budget_bytes,
            )
        except TranslationWorkflowError as exc:
            return RunError(exc.code, str(exc))
        entries: list[dict[str, JsonValue]] = []
        for ordinal, window in enumerate(windows):
            window_id = f"{artifact_prefix}/windows/{ordinal:04d}"
            candidate_id = f"glossary/{window_id}.json"
            candidate_path = context.working.find_candidate(candidate_id)
            window_ref = context.artifacts.find(window_id)
            if candidate_path is None and window_ref is not None:
                window_output = _read_json_artifact(
                    context, window_ref, "glossary window"
                )
            else:
                request = LLMRequest(
                    _task_id(
                        "glossary",
                        {
                            "inventory_digest": inventory["inventory_digest"],
                            "term_ids": [item["term_id"] for item in window],
                            "target_language": target_language,
                            "window_ordinal": ordinal,
                            "prompt_contract": GLOSSARY_PROMPT_VERSION,
                            "input_budget_bytes": term_input_budget_bytes,
                        },
                    ),
                    glossary_prompt(
                        terms=window,
                        target_language=target_language,
                        window_ordinal=ordinal,
                    ),
                    JsonOutput(GLOSSARY_SCHEMA, repair="format"),
                    model,
                )
                generated = _validated_generation(
                    self.task_service,
                    context,
                    request,
                    validator=lambda value: _validated_glossary_document(
                        value, window
                    ),
                    candidate_id=candidate_id,
                    resume_input=resume_input,
                    options=execution,
                    stopped_message="glossary generation stopped",
                )
                if isinstance(generated, (Paused, RunError)):
                    return generated
                if isinstance(generated, _InvalidGeneratedOutput):
                    return _output_supervision(
                        context,
                        artifact_prefix=artifact_prefix,
                        stage=f"glossary-{ordinal:04d}",
                        error=generated.error,
                        candidate_path=generated.candidate_path,
                    )
                window_output = generated
                context.artifacts.publish_json(window_id, window_output)
            try:
                entries.extend(
                    _validate_glossary_window(window_output, window)
                )
            except TranslationWorkflowError as exc:
                error_path = candidate_path or (
                    context.run_directory / window_ref.relative_path
                )
                return _candidate_run_error(exc, error_path)
        result = GlossaryResult(
            document_digest=source.document_digest,
            source_digest=source.source_digest,
            target_language=target_language,
            approx_count=approx_count,
            inventory_digest=_string(inventory, "inventory_digest"),
            entries=tuple(entries),
        )
        context.artifacts.publish_json(artifact_id, result.to_document())
        return result

    def translate_blocks(
        self,
        context: RunContext,
        source: TranslationSource,
        *,
        language: LanguageResult,
        glossary: GlossaryResult,
        target_language: str,
        model: ModelSelection = ModelSelection(),
        execution: LLMExecutionOptions = LLMExecutionOptions(),
        resume_input: ResumeInput | None = None,
        input_budget_bytes: int = 32_000,
        block_ids: Sequence[str] | None = None,
        artifact_prefix: str = "translation",
    ) -> TranslationResult | Paused | RunError:
        _validate_language_binding(language, source, target_language)
        _validate_glossary_binding(glossary, source, target_language)
        all_blocks = source_blocks(source)
        try:
            blocks = _select_blocks(all_blocks, block_ids)
        except TranslationWorkflowError as exc:
            return RunError(exc.code, str(exc))
        coverage = "document" if block_ids is None else "selection"
        artifact_id = f"{artifact_prefix}/result"
        existing = context.artifacts.find(artifact_id)
        if existing is not None:
            result = TranslationResult.from_document(
                _read_json_artifact(context, existing, "translation result")
            )
            _validate_translation_result(
                context,
                result,
                source,
                language,
                target_language,
                blocks,
                expected_coverage=coverage,
            )
            return result
        if language.mode == "skipped":
            result = TranslationResult(
                source_language=language.language_tag,
                target_language=target_language,
                mode="skipped",
                coverage=coverage,
                layer=Layer(
                    source_identity_from_rich_document(source.rich),
                    "arc-translate",
                    (),
                ),
                revision_artifacts=(),
            )
            context.artifacts.publish_json(artifact_id, result.to_document())
            return result
        try:
            windows = _translation_windows(
                _model_translation_blocks(blocks),
                glossary=glossary.entries,
                target_language=target_language,
                language=language,
                budget_bytes=input_budget_bytes,
            )
        except TranslationWorkflowError as exc:
            return RunError(exc.code, str(exc))
        translations: list[dict[str, str]] = []
        for ordinal, window in enumerate(windows):
            draft_id = f"{artifact_prefix}/windows/{ordinal:04d}/draft"
            accepted_id = f"{artifact_prefix}/windows/{ordinal:04d}/accepted"
            accepted_ref = context.artifacts.find(accepted_id)
            if accepted_ref is not None:
                accepted_doc = _read_json_artifact(
                    context, accepted_ref, "accepted translation window"
                )
                try:
                    accepted = _validate_accepted_window(accepted_doc, window)
                except TranslationWorkflowError as exc:
                    return RunError(exc.code, str(exc))
                translations.extend(accepted)
                continue
            candidate_id = f"translation/{draft_id}.json"
            candidate_path = context.working.find_candidate(candidate_id)
            draft_ref = context.artifacts.find(draft_id)
            if candidate_path is None and draft_ref is not None:
                draft_doc = _read_json_artifact(
                    context, draft_ref, "translation draft"
                )
            else:
                request = LLMRequest(
                    _task_id(
                        "translation",
                        {
                            "document_digest": source.document_digest,
                            "block_ids": [item["block_id"] for item in window],
                            "target_language": target_language,
                            "glossary_digest": _digest(
                                glossary.to_document()
                            ),
                            "window_ordinal": ordinal,
                            "prompt_contract": TRANSLATION_PROMPT_VERSION,
                            "input_budget_bytes": input_budget_bytes,
                        },
                    ),
                    translation_prompt(
                        blocks=[prompt_block(item) for item in window],
                        glossary=_window_glossary(window, glossary.entries),
                        target_language=target_language,
                        language_result=language.to_document(),
                        window_ordinal=ordinal,
                    ),
                    JsonOutput(TRANSLATION_SCHEMA, repair="format"),
                    model,
                )
                generated = _validated_generation(
                    self.task_service,
                    context,
                    request,
                    validator=lambda value: _validated_draft_document(
                        value, window
                    ),
                    candidate_id=candidate_id,
                    resume_input=resume_input,
                    options=execution,
                    stopped_message="block translation stopped",
                )
                if isinstance(generated, (Paused, RunError)):
                    return generated
                if isinstance(generated, _InvalidGeneratedOutput):
                    return _output_supervision(
                        context,
                        artifact_prefix=artifact_prefix,
                        stage=f"draft-{ordinal:04d}",
                        error=generated.error,
                        candidate_path=generated.candidate_path,
                    )
                draft_doc = generated
                context.artifacts.publish_json(draft_id, draft_doc)
            try:
                draft = _validate_draft_window(draft_doc, window)
            except TranslationWorkflowError as exc:
                error_path = candidate_path or (
                    context.run_directory / draft_ref.relative_path
                )
                return _candidate_run_error(exc, error_path)
            review_text = review_prompt(
                blocks=[prompt_block(item) for item in window],
                translations=draft,
                glossary=_window_glossary(window, glossary.entries),
                target_language=target_language,
                window_ordinal=ordinal,
            )
            if len(review_text.encode("utf-8")) > input_budget_bytes:
                return RunError(
                    "translation_review_exceeds_input_budget",
                    f"review window {ordinal} exceeds the "
                    f"{input_budget_bytes}-byte translation input budget",
                )
            review_request = LLMRequest(
                _task_id(
                    "translation-review",
                    {
                        "document_digest": source.document_digest,
                        "block_ids": [item["block_id"] for item in window],
                        "draft_digest": _digest(draft),
                        "target_language": target_language,
                        "window_ordinal": ordinal,
                        "prompt_contract": REVIEW_PROMPT_VERSION,
                    },
                ),
                review_text,
                JsonOutput(REVIEW_SCHEMA, repair="format"),
                model,
            )
            review_generated = _validated_generation(
                self.task_service,
                context,
                review_request,
                validator=lambda value: _apply_review(value, draft, window),
                candidate_id=(
                    f"translation/{artifact_prefix}/windows/"
                    f"{ordinal:04d}/review.json"
                ),
                resume_input=resume_input,
                options=execution,
                stopped_message="translation review stopped",
            )
            if isinstance(review_generated, Paused):
                return review_generated
            review_error: tuple[str, str] | None = None
            reviewed: list[dict[str, str]] | None = None
            if isinstance(review_generated, RunError):
                review_error = (
                    review_generated.code,
                    review_generated.message,
                )
            elif isinstance(review_generated, _InvalidGeneratedOutput):
                review_error = (
                    review_generated.error.code,
                    str(review_generated.error),
                )
            else:
                reviewed = review_generated
            if review_error is not None:
                supervision = _review_supervision(
                    context,
                    artifact_prefix=artifact_prefix,
                    ordinal=ordinal,
                    draft=draft,
                    error_code=review_error[0],
                    error_message=review_error[1],
                )
                if isinstance(supervision, Paused):
                    return supervision
                reviewed = supervision
            assert reviewed is not None
            accepted_doc = {"translations": reviewed}
            context.artifacts.publish_json(accepted_id, accepted_doc)
            translations.extend(reviewed)
        merged_translations = _merge_structural_figure_translations(
            blocks,
            translations,
        )
        result = _publish_translation_result(
            context,
            source,
            translations=merged_translations,
            source_language=language.language_tag,
            target_language=target_language,
            artifact_prefix=artifact_prefix,
            coverage=coverage,
        )
        _validate_translation_result(
            context,
            result,
            source,
            language,
            target_language,
            blocks,
            expected_coverage=coverage,
        )
        context.artifacts.publish_json(artifact_id, result.to_document())
        return result


class TranslationWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _InvalidGeneratedOutput:
    error: TranslationWorkflowError
    candidate_path: Path


def _candidate_run_error(
    error: TranslationWorkflowError, path: Path
) -> RunError:
    return RunError(
        error.code,
        f"{error}; editable candidate: {path}",
        {"candidate_path": str(path)},
    )


def _validated_generation(
    service: Any,
    context: RunContext,
    request: LLMRequest,
    *,
    validator: Callable[[Any], Any],
    candidate_id: str,
    resume_input: ResumeInput | None,
    options: LLMExecutionOptions,
    stopped_message: str,
) -> Any | Paused | RunError | _InvalidGeneratedOutput:
    """Retry one model-correctable identity/coverage failure, then pause."""

    candidate_path = context.working.find_candidate(candidate_id)
    if candidate_path is not None:
        candidate = context.working.read_candidate_json(candidate_id)
        try:
            return validator(candidate)
        except TranslationWorkflowError as exc:
            return _InvalidGeneratedOutput(exc, candidate_path)

    first_candidate_id = _attempt_candidate_id(candidate_id)
    first_candidate_path = context.working.find_candidate(first_candidate_id)
    feedback: TranslationWorkflowError | None = None
    if first_candidate_path is not None:
        first_candidate = context.working.read_candidate_json(first_candidate_id)
        try:
            validated = validator(first_candidate)
            context.working.write_candidate_json(
                candidate_id, first_candidate
            )
            return validated
        except TranslationWorkflowError as exc:
            feedback = exc

    attempt = 2 if feedback is not None else 1
    current_request = (
        _semantic_retry_request(request, feedback)
        if feedback is not None
        else request
    )
    while True:
        outcome = _execute(
            service,
            context,
            current_request,
            resume_input=resume_input,
            options=options,
        )
        if isinstance(outcome, LLMPaused):
            return Paused(_awaiting(outcome))
        if isinstance(outcome, LLMFailed):
            return _run_error(outcome)
        if isinstance(outcome, LLMStopped):
            raise StoppedError(stopped_message)
        assert isinstance(outcome, LLMCompleted)
        try:
            validated = validator(outcome.value)
        except TranslationWorkflowError as exc:
            document = _generated_candidate_document(outcome.value)
            if attempt == 1:
                context.working.write_candidate_json(
                    first_candidate_id, document
                )
                feedback = exc
                attempt = 2
                current_request = _semantic_retry_request(request, exc)
                continue
            path = context.working.write_candidate_json(
                candidate_id, document
            )
            return _InvalidGeneratedOutput(exc, path)
        context.working.write_candidate_json(
            candidate_id, _generated_candidate_document(outcome.value)
        )
        return validated


def _attempt_candidate_id(candidate_id: str) -> str:
    if candidate_id.endswith(".json"):
        return f"{candidate_id[:-5]}-attempt-1.json"
    return f"{candidate_id}-attempt-1"


def _semantic_retry_request(
    request: LLMRequest, error: TranslationWorkflowError
) -> LLMRequest:
    message = str(error)
    bounded_message = message[:500]
    error_digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    prompt = request.prompt
    marker = "\n\nInput JSON:\n"
    if marker in prompt:
        prefix, payload = prompt.split(marker, 1)
        prompt = (
            f"{prefix}\n\nRetry after a machine-checkable output-contract "
            f"failure ({error.code}): {bounded_message}. Generate the "
            "complete output "
            "again; do not narrow or change the scientific content."
            f"{marker}{payload}"
        )
    else:
        prompt = (
            f"{prompt}\n\nRetry after a machine-checkable output-contract "
            f"failure ({error.code}): {bounded_message}. Generate the "
            "complete output "
            "again; do not narrow or change the scientific content."
        )
    return LLMRequest(
        _task_id(
            "semantic-retry",
            {
                "task_id": request.task_id,
                "error_code": error.code,
                "error_message": bounded_message,
                "error_digest": error_digest,
            },
        ),
        prompt,
        request.output,
        request.model,
        request.session,
        request.inputs,
    )


def _generated_candidate_document(value: Any) -> dict[str, JsonValue]:
    public = _public_value(value)
    if isinstance(public, Mapping):
        return dict(public)
    return {"generated_value": public}


def _output_supervision(
    context: RunContext,
    *,
    artifact_prefix: str,
    stage: str,
    error: TranslationWorkflowError,
    candidate_path: Path,
) -> Paused:
    digest = _digest(
        {
            "stage": stage,
            "candidate_path": str(candidate_path),
        }
    )[:24]
    resume_key = f"output-{digest}"
    request_id = f"{artifact_prefix}/output-supervision/{stage}"
    request_ref = context.artifacts.find(request_id)
    if request_ref is None:
        request_ref = context.artifacts.publish_json(
            request_id,
            {
                "schema_version": OUTPUT_SUPERVISION_SCHEMA,
                "resume_key": resume_key,
                "reason": error.code,
                "message": str(error),
                "candidate_path": str(candidate_path),
                "automatic_retry_exhausted": True,
                "output_attempts": 2,
            },
        )
    return Paused(
        Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            resume_key,
            False,
            request_ref,
            None,
            {
                "stage": stage,
                "code": error.code,
                "candidate_path": str(candidate_path),
                "automatic_retry_exhausted": True,
                "output_attempts": 2,
            },
        )
    )


def outer_resume_input(context: RunContext) -> ResumeInput | None:
    if context.resume_input is None:
        return None
    try:
        return decode_resume_input(context.resume_input)
    except Exception as exc:
        if context.resume_input.get("schema_version") == RESUME_SCHEMA_VERSION:
            raise TranslationWorkflowError(
                "llm_resume_input_invalid", "Malformed arc-llm resume input"
            ) from exc
        return None


def _execute(
    service: Any,
    context: RunContext,
    request: LLMRequest,
    *,
    resume_input: ResumeInput | None,
    options: LLMExecutionOptions,
) -> Any:
    if resume_input is not None and resume_input_matches(request, resume_input):
        return service.execute_or_resume(
            context, request, input=resume_input, options=options
        )
    return service.execute_or_resume(context, request, options=options)


def _validate_language_output(value: Any) -> dict[str, Any]:
    document = _object(value, "language output")
    _require_fields(
        document,
        {"language_tag", "classification", "confidence"},
        "language output",
    )
    tag = _string(document, "language_tag").strip()
    classification = _string(document, "classification")
    confidence = document["confidence"]
    if not tag or classification not in {"known", "mixed", "unknown"}:
        raise TranslationWorkflowError(
            "language_result_invalid", "language classification is invalid"
        )
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= float(confidence) <= 1
    ):
        raise TranslationWorkflowError(
            "language_result_invalid", "language confidence is invalid"
        )
    return {
        "language_tag": tag,
        "classification": classification,
        "confidence": float(confidence),
    }


_TERM_IDENTITY_FIELDS = (
    "term_id",
    "term",
    "aliases",
    "occurrence_count",
    "source_refs",
    "matched_sentences",
)
_GLOSSARY_ENTRY_FIELDS = {
    *_TERM_IDENTITY_FIELDS,
    "preferred_translation",
    "target_definition",
}


def _keyword_inventory_document(value: Any) -> dict[str, JsonValue]:
    raw = _public_value(value)
    if not isinstance(raw, Mapping):
        raise TranslationWorkflowError(
            "keyword_inventory_invalid", "keyword inventory must be an object"
        )
    document = dict(raw)
    return document  # type: ignore[return-value]


def _public_value(value: Any) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    if dataclasses.is_dataclass(value):
        return _public_value(dataclasses.asdict(value))
    to_document = getattr(value, "to_document", None)
    if callable(to_document):
        return _public_value(to_document())
    raise TranslationWorkflowError(
        "keyword_inventory_invalid",
        f"keyword value is not JSON-compatible: {type(value).__name__}",
    )


def _validate_keyword_inventory(
    value: Mapping[str, Any],
    *,
    source: TranslationSource,
    approx_count: int,
) -> None:
    fields = {
        "schema_version",
        "document_digest",
        "source_digest",
        "approx_count",
        "planned_count",
        "returned_count",
        "terms",
        "inventory_digest",
        "warnings",
    }
    _require_fields(value, fields, "keyword inventory")
    if (
        _string(value, "schema_version") != "arc.paper.keyword_result.v1"
        or _string(value, "document_digest") != source.document_digest
        or _string(value, "source_digest") != source.source_digest
        or _integer(value, "approx_count") != approx_count
        or _integer(value, "planned_count")
        != (3 * approx_count + 1) // 2
    ):
        raise TranslationWorkflowError(
            "keyword_inventory_binding_mismatch",
            "keyword inventory does not match the translation source",
        )
    terms = _mapping_list(value["terms"], "keyword terms")
    warnings = value["warnings"]
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) for item in warnings
    ):
        raise TranslationWorkflowError(
            "keyword_inventory_invalid",
            "keyword warnings must be an array of strings",
        )
    if _integer(value, "returned_count") != len(terms):
        raise TranslationWorkflowError(
            "keyword_inventory_invalid",
            "keyword returned_count does not match terms",
        )
    term_ids: set[str] = set()
    for item in terms:
        _require_fields(item, set(_TERM_IDENTITY_FIELDS), "keyword term")
        term_id = _string(item, "term_id")
        if (
            not term_id
            or term_id in term_ids
            or not _string(item, "term").strip()
            or not isinstance(item["aliases"], list)
            or any(not isinstance(alias, str) for alias in item["aliases"])
            or isinstance(item["occurrence_count"], bool)
            or not isinstance(item["occurrence_count"], int)
            or item["occurrence_count"] < 0
            or not isinstance(item["source_refs"], list)
            or any(
                not isinstance(source_ref, str)
                for source_ref in item["source_refs"]
            )
            or not isinstance(item["matched_sentences"], list)
        ):
            raise TranslationWorkflowError(
                "keyword_inventory_invalid", "keyword term identity is invalid"
            )
        term_ids.add(term_id)
        for sentence in item["matched_sentences"]:
            if (
                not isinstance(sentence, Mapping)
                or set(sentence)
                != {
                    "text",
                    "section_id",
                    "page_number",
                    "matched_surface",
                    "clipped",
                }
                or not isinstance(sentence["text"], str)
                or not isinstance(sentence["section_id"], str)
                or not isinstance(sentence["matched_surface"], str)
                or not isinstance(sentence["clipped"], bool)
                or (
                    sentence["page_number"] is not None
                    and (
                        isinstance(sentence["page_number"], bool)
                        or not isinstance(sentence["page_number"], int)
                        or sentence["page_number"] < 1
                    )
                )
            ):
                raise TranslationWorkflowError(
                    "keyword_inventory_invalid",
                    "matched sentence grounding is invalid",
                )
    inventory_digest = _string(value, "inventory_digest")
    if not _is_sha256(inventory_digest):
        raise TranslationWorkflowError(
            "keyword_inventory_invalid", "inventory_digest is invalid"
        )


def _glossary_windows(
    terms: Sequence[Mapping[str, Any]],
    *,
    target_language: str,
    budget_bytes: int,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    windows: list[tuple[Mapping[str, Any], ...]] = []
    current: list[Mapping[str, Any]] = []
    for term in terms:
        candidate = [*current, term]
        if (
            len(
                glossary_prompt(
                    terms=candidate,
                    target_language=target_language,
                    window_ordinal=len(windows),
                ).encode("utf-8")
            )
            <= budget_bytes
        ):
            current = candidate
            continue
        if current:
            windows.append(tuple(current))
            current = [term]
        else:
            current = [term]
        if (
            len(
                glossary_prompt(
                    terms=current,
                    target_language=target_language,
                    window_ordinal=len(windows),
                ).encode("utf-8")
            )
            > budget_bytes
        ):
            raise TranslationWorkflowError(
                "glossary_term_exceeds_input_budget",
                f"term {term.get('term_id', '<unknown>')} exceeds the "
                f"{budget_bytes}-byte glossary input budget",
            )
    if current:
        windows.append(tuple(current))
    return tuple(windows)


def _validate_glossary_window(
    value: Mapping[str, Any], terms: Sequence[Mapping[str, Any]]
) -> list[dict[str, JsonValue]]:
    _require_fields(value, {"entries"}, "glossary window")
    entries = _mapping_list(value["entries"], "glossary window entries")
    if len(entries) != len(terms):
        raise TranslationWorkflowError(
            "glossary_coverage_invalid",
            "glossary window must cover every supplied term",
        )
    output: list[dict[str, JsonValue]] = []
    for entry, term in zip(entries, terms, strict=True):
        _require_fields(
            entry,
            {"term_id", "preferred_translation", "target_definition"},
            "glossary entry",
        )
        if entry["term_id"] != term["term_id"]:
            raise TranslationWorkflowError(
                "glossary_term_identity_invalid",
                "glossary changed term coverage, order, or identity",
            )
        if (
            not isinstance(entry["preferred_translation"], str)
            or not entry["preferred_translation"].strip()
            or not isinstance(entry["target_definition"], str)
            or not entry["target_definition"].strip()
        ):
            raise TranslationWorkflowError(
                "glossary_content_invalid",
                "glossary translations and target definitions must be non-empty",
            )
        output.append(
            {
                **dict(term),
                "preferred_translation": entry["preferred_translation"],
                "target_definition": entry["target_definition"],
            }
        )  # type: ignore[arg-type]
    return output


def _validated_glossary_document(
    value: Any, terms: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    document = _object(value, "glossary window")
    _validate_glossary_window(document, terms)
    return document


def _validate_glossary_entries(
    entries: Sequence[Mapping[str, Any]],
) -> None:
    term_ids: set[str] = set()
    for entry in entries:
        _require_fields(entry, _GLOSSARY_ENTRY_FIELDS, "glossary entry")
        term_id = _string(entry, "term_id")
        if (
            not term_id
            or term_id in term_ids
            or not _string(entry, "term").strip()
            or not isinstance(entry["aliases"], list)
            or any(not isinstance(item, str) for item in entry["aliases"])
            or isinstance(entry["occurrence_count"], bool)
            or not isinstance(entry["occurrence_count"], int)
            or entry["occurrence_count"] < 0
            or not isinstance(entry["source_refs"], list)
            or not isinstance(entry["matched_sentences"], list)
            or not isinstance(entry["preferred_translation"], str)
            or not entry["preferred_translation"].strip()
            or not isinstance(entry["target_definition"], str)
            or not entry["target_definition"].strip()
        ):
            raise ValueError("glossary entry is invalid or duplicated")
        term_ids.add(term_id)


def _translation_windows(
    blocks: Sequence[Mapping[str, Any]],
    *,
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language: LanguageResult,
    budget_bytes: int,
) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    windows: list[tuple[Mapping[str, Any], ...]] = []
    current: list[Mapping[str, Any]] = []
    for block in blocks:
        candidate = [*current, block]
        if (
            _translation_prompt_size(
                candidate,
                glossary=glossary,
                target_language=target_language,
                language=language,
                ordinal=len(windows),
            )
            <= budget_bytes
            and (
                len(candidate) == 1
                or _translation_review_estimate_size(
                    candidate,
                    glossary=glossary,
                    target_language=target_language,
                    ordinal=len(windows),
                )
                <= budget_bytes
            )
        ):
            current = candidate
            continue
        if current:
            windows.append(tuple(current))
            current = [block]
        else:
            current = [block]
        if (
            _translation_prompt_size(
                current,
                glossary=glossary,
                target_language=target_language,
                language=language,
                ordinal=len(windows),
            )
            > budget_bytes
        ):
            raise TranslationWorkflowError(
                "translation_block_exceeds_input_budget",
                f"block {block.get('block_id', '<unknown>')} exceeds the "
                f"{budget_bytes}-byte translation input budget",
            )
    if current:
        windows.append(tuple(current))
    return tuple(windows)


def _select_blocks(
    blocks: Sequence[Mapping[str, Any]],
    block_ids: Sequence[str] | None,
) -> tuple[Mapping[str, Any], ...]:
    if block_ids is None:
        return tuple(blocks)
    if (
        isinstance(block_ids, (str, bytes))
        or not isinstance(block_ids, Sequence)
        or not block_ids
        or any(not isinstance(item, str) or not item for item in block_ids)
        or len(set(block_ids)) != len(block_ids)
    ):
        raise TranslationWorkflowError(
            "block_selector_invalid",
            "block_ids must be a non-empty unique sequence of block IDs",
        )
    requested = set(block_ids)
    available = {str(item["block_id"]) for item in blocks}
    missing = requested - available
    if missing:
        raise TranslationWorkflowError(
            "block_selector_invalid",
            f"block_ids contain unknown source IDs: {sorted(missing)!r}",
        )
    return tuple(item for item in blocks if str(item["block_id"]) in requested)


def _model_translation_blocks(
    blocks: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        block for block in blocks if not _is_structural_figure(block)
    )


def _is_structural_figure(block: Mapping[str, Any]) -> bool:
    if str(block.get("kind")) != "figure":
        return False
    payload = block.get("payload")
    if not isinstance(payload, Mapping):
        raise TranslationWorkflowError(
            "source_block_invalid",
            "source block payload must be an object",
        )
    return not (
        str(payload.get("caption", "")).strip()
        or str(payload.get("alt_text", "")).strip()
    )


def _is_nonlinguistic_media_block(block: Mapping[str, Any]) -> bool:
    """Return whether a source media block has no language-bearing content."""

    return _is_structural_figure(block)


def _is_nonlinguistic_media_translation(
    block: Mapping[str, Any], text: str
) -> bool:
    if not _is_nonlinguistic_media_block(block):
        return False
    payload = block.get("payload")
    assert isinstance(payload, Mapping)
    marker = text.strip()
    structural_markers = {
        STRUCTURAL_FIGURE_PLACEHOLDER,
        str(payload.get("target", "")).strip(),
        str(payload.get("asset_digest", "")).strip(),
    }
    structural_markers.discard("")
    return marker in structural_markers


def _merge_structural_figure_translations(
    blocks: Sequence[Mapping[str, Any]],
    model_translations: Sequence[Mapping[str, str]],
) -> tuple[Mapping[str, str], ...]:
    translated_by_id = {
        str(item["block_id"]): dict(item) for item in model_translations
    }
    merged: list[Mapping[str, str]] = []
    for block in blocks:
        block_id = str(block["block_id"])
        if _is_structural_figure(block):
            merged.append(
                {
                    "block_id": block_id,
                    "text": STRUCTURAL_FIGURE_PLACEHOLDER,
                }
            )
            continue
        translation = translated_by_id.pop(block_id, None)
        if translation is None:
            raise TranslationWorkflowError(
                "translation_coverage_invalid",
                f"translation omitted source block {block_id}",
            )
        merged.append(translation)
    if translated_by_id:
        raise TranslationWorkflowError(
            "translation_coverage_invalid",
            "translation contains unknown or duplicate source blocks",
        )
    return tuple(merged)


def _window_glossary(
    blocks: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    text = "\n".join(block_text(item) for item in blocks).casefold()
    return tuple(
        {
            "term_id": item["term_id"],
            "term": item["term"],
            "aliases": item["aliases"],
            "preferred_translation": item["preferred_translation"],
            "target_definition": item["target_definition"],
        }
        for item in entries
        if isinstance(item.get("term"), str)
        and str(item["term"]).casefold() in text
    )


def _translation_prompt_size(
    blocks: Sequence[Mapping[str, Any]],
    *,
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    language: LanguageResult,
    ordinal: int,
) -> int:
    return len(
        translation_prompt(
            blocks=[prompt_block(item) for item in blocks],
            glossary=_window_glossary(blocks, glossary),
            target_language=target_language,
            language_result=language.to_document(),
            window_ordinal=ordinal,
        ).encode("utf-8")
    )


def _translation_review_estimate_size(
    blocks: Sequence[Mapping[str, Any]],
    *,
    glossary: Sequence[Mapping[str, Any]],
    target_language: str,
    ordinal: int,
) -> int:
    return len(
        review_prompt(
            blocks=[prompt_block(item) for item in blocks],
            translations=[
                {
                    "block_id": str(item["block_id"]),
                    "text": block_text(item) or "translated block",
                }
                for item in blocks
            ],
            glossary=_window_glossary(blocks, glossary),
            target_language=target_language,
            window_ordinal=ordinal,
        ).encode("utf-8")
    )


def _validate_draft_window(
    value: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    _require_fields(value, {"translations"}, "translation draft")
    translations = _mapping_list(
        value["translations"], "translation draft entries"
    )
    expected = [str(item["block_id"]) for item in blocks]
    if [item.get("block_id") for item in translations] != expected:
        raise TranslationWorkflowError(
            "translation_coverage_invalid",
            "translation block IDs must exactly match source order",
        )
    output: list[dict[str, str]] = []
    for translated, block in zip(translations, blocks, strict=True):
        _require_fields(
            translated,
            {"block_id", "text"},
            "translated block",
        )
        text = translated["text"]
        if not isinstance(text, str):
            raise TranslationWorkflowError(
                "translation_coverage_invalid", "translation text must be a string"
            )
        try:
            validate_translation_text(text, block)
        except TranslationSourceError as exc:
            raise TranslationWorkflowError(exc.code, str(exc)) from exc
        output.append({"block_id": str(translated["block_id"]), "text": text})
    return output


def _validated_draft_document(
    value: Any, blocks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    document = _object(value, "translation draft")
    _validate_draft_window(document, blocks)
    return document


def _validate_accepted_window(
    value: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    _require_fields(value, {"translations"}, "accepted translation window")
    translations = _mapping_list(
        value["translations"], "accepted translation entries"
    )
    expected = [str(item["block_id"]) for item in blocks]
    if [item.get("block_id") for item in translations] != expected:
        raise TranslationWorkflowError(
            "translation_coverage_invalid",
            "accepted translations must exactly match source order",
        )
    output: list[dict[str, str]] = []
    for translated, block in zip(translations, blocks, strict=True):
        _require_fields(translated, {"block_id", "text"}, "accepted translation")
        text = translated["text"]
        if not isinstance(text, str):
            raise TranslationWorkflowError(
                "translation_coverage_invalid", "translation text must be a string"
            )
        try:
            validate_translation_text(text, block)
        except TranslationSourceError as exc:
            raise TranslationWorkflowError(exc.code, str(exc)) from exc
        output.append({"block_id": str(translated["block_id"]), "text": text})
    return output


def _apply_review(
    value: Any,
    draft: Sequence[Mapping[str, str]],
    blocks: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    document = _object(value, "translation review")
    _require_fields(
        document, {"translation_patches", "summary"}, "translation review"
    )
    patches = _mapping_list(
        document["translation_patches"], "translation review patches"
    )
    if not isinstance(document["summary"], str) or not document["summary"].strip():
        raise TranslationWorkflowError(
            "translation_review_invalid", "review summary must be non-empty"
        )
    allowed = {str(item["block_id"]) for item in draft}
    replacements: dict[str, str] = {}
    for patch in patches:
        _require_fields(
            patch, {"block_id", "replacement"}, "translation review patch"
        )
        block_id = _string(patch, "block_id")
        replacement = _string(patch, "replacement")
        if block_id not in allowed or block_id in replacements:
            raise TranslationWorkflowError(
                "translation_review_invalid",
                "review patch IDs must be unique existing block IDs",
            )
        replacements[block_id] = replacement
    reviewed = [
        {
            "block_id": str(item["block_id"]),
            "text": replacements.get(str(item["block_id"]), str(item["text"])),
        }
        for item in draft
    ]
    return _validate_accepted_window({"translations": reviewed}, blocks)


def _review_supervision(
    context: RunContext,
    *,
    artifact_prefix: str,
    ordinal: int,
    draft: Sequence[Mapping[str, str]],
    error_code: str,
    error_message: str,
) -> list[dict[str, str]] | Paused:
    digest = _digest(
        {
            "window_ordinal": ordinal,
            "draft": list(draft),
            "error_code": error_code,
        }
    )[:24]
    resume_key = f"review-{digest}"
    value = context.resume_input
    if value is not None and value.get("resume_key") == resume_key:
        if set(value) != {"schema_version", "resume_key", "action"} or (
            value.get("schema_version") != REVIEW_SUPERVISION_SCHEMA
            or value.get("action") != "accept_pre_review"
        ):
            raise TranslationWorkflowError(
                "review_supervision_invalid",
                "only accept_pre_review is supported for a failed review",
            )
        return [dict(item) for item in draft]
    request_id = f"{artifact_prefix}/windows/{ordinal:04d}/review-supervision"
    request_ref = context.artifacts.find(request_id)
    if request_ref is None:
        request_ref = context.artifacts.publish_json(
            request_id,
            {
                "schema_version": REVIEW_SUPERVISION_SCHEMA,
                "resume_key": resume_key,
                "reason": error_code,
                "message": error_message,
                "allowed_actions": ["accept_pre_review"],
            },
        )
    return Paused(
        Awaiting(
            ResumeReason.SUPERVISION_REQUIRED,
            resume_key,
            True,
            request_ref,
            REVIEW_SUPERVISION_SCHEMA,
            {"window_ordinal": ordinal, "code": error_code},
        )
    )


def _validate_language_binding(
    result: LanguageResult,
    source: TranslationSource,
    target_language: str,
) -> None:
    if (
        result.document_digest != source.document_digest
        or result.source_digest != source.source_digest
        or result.target_language != target_language
    ):
        raise TranslationWorkflowError(
            "language_result_binding_mismatch",
            "language result does not match source and target language",
        )


def _validate_glossary_binding(
    result: GlossaryResult,
    source: TranslationSource,
    target_language: str,
) -> None:
    if (
        result.document_digest != source.document_digest
        or result.source_digest != source.source_digest
        or result.target_language != target_language
    ):
        raise TranslationWorkflowError(
            "glossary_result_binding_mismatch",
            "glossary result does not match source and target language",
        )


def _validate_translation_result(
    context: RunContext,
    result: TranslationResult,
    source: TranslationSource,
    language: LanguageResult,
    target_language: str,
    blocks: Sequence[Mapping[str, Any]],
    *,
    expected_coverage: str,
) -> None:
    if (
        result.layer.source
        != source_identity_from_rich_document(source.rich)
        or result.layer.producer != "arc-translate"
        or result.source_language != language.language_tag
        or result.target_language != target_language
        or result.mode != language.mode
        or result.coverage != expected_coverage
    ):
        raise TranslationWorkflowError(
            "translation_result_binding_mismatch",
            "translation result does not match source, language, or target",
        )
    if result.mode == "skipped":
        if result.revision_artifacts:
            raise TranslationWorkflowError(
                "translation_coverage_invalid",
                "skipped translation result must be empty",
            )
        return
    expected = [
        str(item["block_id"])
        for item in blocks
        if not _is_nonlinguistic_media_block(item)
    ]
    actual: list[str] = []
    source_blocks_by_id = {
        block.block_id: block for block in source.rich.blocks
    }
    for item in result.revision_artifacts:
        try:
            payload = context.artifacts.read_bytes(item.artifact).decode(
                "utf-8"
            )
            revision = decode_fragment_revision(
                payload,
                filename=Path(item.revision.path).name,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise TranslationWorkflowError(
                "translation_revision_invalid",
                "translation revision artifact is unreadable or invalid",
            ) from exc
        if (
            revision.semantic_digest != item.revision.semantic_digest
            or revision.fragment_id != item.revision.fragment_id
            or revision.source != result.layer.source
            or revision.priority != 10
            or revision.role != "translation"
            or revision.language != target_language
            or revision.anchor.kind is not AnchorKind.BLOCK
        ):
            raise TranslationWorkflowError(
                "translation_revision_binding_mismatch",
                "translation revision does not match its result manifest",
            )
        block_id = revision.anchor.target_id
        rich_block = source_blocks_by_id.get(block_id)
        if (
            rich_block is None
            or revision.anchor.related_block_ids != (block_id,)
            or revision.anchor.related_blocks[0]
            != anchor_block_from_rich_block(rich_block)
        ):
            raise TranslationWorkflowError(
                "translation_revision_binding_mismatch",
                "translation revision anchor does not match its RichDocument block",
            )
        rich_block_document = rich_block_to_document(rich_block)
        try:
            if rich_block.kind is RichBlockKind.CODE:
                expected_code = block_text_to_markdown(
                    rich_block,
                    str(rich_block.payload["text"]),
                )
                if revision.markdown_body != expected_code:
                    raise TranslationSourceError(
                        "translation_source_identity_invalid",
                        f"translation changed code text for {block_id}",
                    )
            else:
                validate_translation_text(
                    revision.markdown_body,
                    rich_block_document,
                )
        except TranslationSourceError as exc:
            raise TranslationWorkflowError(exc.code, str(exc)) from exc
        actual.append(block_id)
    if actual != expected:
        raise TranslationWorkflowError(
            "translation_coverage_invalid",
            "translation result must cover all source blocks in order",
        )


def _publish_translation_result(
    context: RunContext,
    source: TranslationSource,
    *,
    translations: Sequence[Mapping[str, str]],
    source_language: str,
    target_language: str,
    artifact_prefix: str,
    coverage: str,
) -> TranslationResult:
    rich_blocks = {item.block_id: item for item in source.rich.blocks}
    source_identity = source_identity_from_rich_document(source.rich)
    revisions: list[FragmentRevisionRef] = []
    artifacts: list[TranslationRevisionArtifact] = []
    for translated in translations:
        block_id = str(translated["block_id"])
        block = rich_blocks.get(block_id)
        if block is None:
            raise TranslationWorkflowError(
                "translation_coverage_invalid",
                f"translation refers to unknown source block {block_id}",
            )
        block_document = rich_block_to_document(block)
        if _is_nonlinguistic_media_translation(
            block_document, str(translated["text"])
        ):
            continue
        markdown_body = block_text_to_markdown(
            block,
            str(translated["text"]),
        )
        fragment_material = {
            "producer": "arc-translate",
            "source": source_identity.rich_document_digest,
            "block_id": block_id,
            "target_language": target_language,
            "markdown_body": markdown_body,
        }
        fragment_digest = hashlib.sha256(
            canonical_json_bytes(fragment_material)
        ).hexdigest()
        revision = FragmentRevision(
            source=source_identity,
            fragment_id=f"translation-{fragment_digest[:32]}",
            revision=1,
            parent_semantic_digest=None,
            anchor=FragmentAnchor(
                AnchorKind.BLOCK,
                block_id,
                (anchor_block_from_rich_block(block),),
            ),
            priority=10,
            role="translation",
            language=target_language,
            title=None,
            citation_ids=(),
            provenance={
                "producer": "arc-translate",
                "source_language": source_language,
                "translation_mode": "enabled",
            },
            markdown_body=markdown_body,
        )
        path = fragment_revision_storage_path(revision)
        reference = fragment_revision_ref(path, revision)
        artifact = context.artifacts.publish_bytes(
            f"{artifact_prefix}/fragments/{revision.fragment_id}/revision-000001",
            encode_fragment_revision(revision).encode("utf-8"),
            media_type="text/markdown",
        )
        revisions.append(reference)
        artifacts.append(TranslationRevisionArtifact(reference, artifact))
    return TranslationResult(
        source_language=source_language,
        target_language=target_language,
        mode="enabled",
        coverage=coverage,
        layer=Layer(source_identity, "arc-translate", tuple(revisions)),
        revision_artifacts=tuple(artifacts),
    )


def _awaiting(outcome: LLMPaused) -> Awaiting:
    return Awaiting(
        outcome.reason,
        outcome.resume_key,
        outcome.input_required,
        outcome.request_ref,
        outcome.response_contract,
        outcome.details,
    )


def _run_error(outcome: LLMFailed) -> RunError:
    return RunError(
        outcome.error.code.value,
        str(outcome.error),
        outcome.error.details,
    )


def _read_json_artifact(
    context: RunContext, ref: Any, description: str
) -> dict[str, Any]:
    try:
        value = json.loads(context.artifacts.read_bytes(ref).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranslationWorkflowError(
            "artifact_invalid", f"{description} is unreadable"
        ) from exc
    return _object(value, description)


def _task_id(prefix: str, semantic: Any) -> str:
    return f"{prefix}-{_digest(semantic)[:24]}"


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _object(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TranslationWorkflowError(
            "model_output_invalid", f"{description} must be an object"
        )
    return dict(value)


def _mapping_list(value: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise TranslationWorkflowError(
            "model_output_invalid",
            f"{description} must be an array of objects",
        )
    return [dict(item) for item in value]


def _require_fields(
    value: Mapping[str, Any], fields: set[str], description: str
) -> None:
    if set(value) != fields:
        raise TranslationWorkflowError(
            "model_output_invalid", f"{description} has invalid fields"
        )


def _string(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise TranslationWorkflowError(
            "model_output_invalid", f"{key} must be a string"
        )
    return item


def _integer(value: Mapping[str, Any], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise TranslationWorkflowError(
            "model_output_invalid", f"{key} must be an integer"
        )
    return item


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


__all__ = [
    "GlossaryResult",
    "KeywordProvider",
    "LanguageResult",
    "TranslationResult",
    "TranslationRevisionArtifact",
    "REVIEW_SUPERVISION_SCHEMA",
    "TranslationWorkflowError",
    "TranslationWorkflowService",
    "outer_resume_input",
]
