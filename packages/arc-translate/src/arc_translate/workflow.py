"""Reusable translation tasks that run inside an existing ``RunContext``."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from arc_jobs import (
    Awaiting,
    Failed,
    JsonValue,
    Paused,
    ResumeReason,
    RunContext,
    RunError,
    StoppedError,
    canonical_json_bytes,
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
    ResumeInput,
    decode_resume_input,
    resume_input_matches,
)
from arc_llm.request import RESUME_SCHEMA_VERSION

from .contracts import (
    BLOCKS_RESULT_SCHEMA,
    GLOSSARY_RESULT_SCHEMA,
    LANGUAGE_RESULT_SCHEMA,
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
    TranslationSourceError,
    block_text,
    deterministic_language_samples,
    prompt_block,
    same_primary_language,
    source_blocks,
    source_identity,
    validate_translation_text,
)


REVIEW_SUPERVISION_SCHEMA = "arc.translate.review_supervision.v1"


class KeywordProvider(Protocol):
    def extract_keywords(
        self,
        context: RunContext,
        source: Any,
        *,
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
class BlocksResult:
    document_digest: str
    source_digest: str
    source_language: str
    target_language: str
    mode: str
    translations: tuple[Mapping[str, str], ...]
    schema_version: str = BLOCKS_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != BLOCKS_RESULT_SCHEMA:
            raise ValueError("unsupported blocks result schema")
        if self.mode not in {"enabled", "skipped"}:
            raise ValueError("blocks result mode is invalid")
        values = tuple(dict(item) for item in self.translations)
        for item in values:
            if (
                set(item) != {"block_id", "text"}
                or not isinstance(item["block_id"], str)
                or not item["block_id"]
                or not isinstance(item["text"], str)
                or not item["text"].strip()
            ):
                raise ValueError("translated block is invalid")
        object.__setattr__(self, "translations", values)

    def to_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "document_digest": self.document_digest,
            "source_digest": self.source_digest,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "mode": self.mode,
            "translations": [dict(item) for item in self.translations],
        }

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "BlocksResult":
        _require_fields(
            value,
            {
                "schema_version",
                "document_digest",
                "source_digest",
                "source_language",
                "target_language",
                "mode",
                "translations",
            },
            "blocks result",
        )
        translations = _mapping_list(
            value["translations"], "translated blocks"
        )
        return cls(
            document_digest=_string(value, "document_digest"),
            source_digest=_string(value, "source_digest"),
            source_language=_string(value, "source_language"),
            target_language=_string(value, "target_language"),
            mode=_string(value, "mode"),
            translations=tuple(translations),  # type: ignore[arg-type]
            schema_version=_string(value, "schema_version"),
        )


WorkflowResult: TypeAlias = (
    LanguageResult | GlossaryResult | BlocksResult | Paused | RunError
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
        outcome = _execute(
            self.task_service,
            context,
            request,
            resume_input=resume_input,
            options=execution,
        )
        if isinstance(outcome, LLMPaused):
            return Paused(_awaiting(outcome))
        if isinstance(outcome, LLMFailed):
            return _run_error(outcome)
        if isinstance(outcome, LLMStopped):
            raise StoppedError("language detection stopped")
        assert isinstance(outcome, LLMCompleted)
        value = _validate_language_output(outcome.value)
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
            keyword_source = (
                source.parsed if source.parsed is not None else source.rich
            )
            assert keyword_source is not None
            try:
                keyword_outcome = self.keyword_provider.extract_keywords(
                    context,
                    keyword_source,
                    approx_count=approx_count,
                    model=model,
                    resume_input=(
                        context.resume_input
                        if context.resume_input is not None
                        else None
                    ),
                    options=execution,
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
            window_ref = context.artifacts.find(window_id)
            if window_ref is not None:
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
                outcome = _execute(
                    self.task_service,
                    context,
                    request,
                    resume_input=resume_input,
                    options=execution,
                )
                if isinstance(outcome, LLMPaused):
                    return Paused(_awaiting(outcome))
                if isinstance(outcome, LLMFailed):
                    return _run_error(outcome)
                if isinstance(outcome, LLMStopped):
                    raise StoppedError("glossary generation stopped")
                assert isinstance(outcome, LLMCompleted)
                window_output = _object(
                    outcome.value, "glossary window"
                )
                try:
                    _validate_glossary_window(window_output, window)
                except TranslationWorkflowError as exc:
                    return RunError(exc.code, str(exc))
                context.artifacts.publish_json(window_id, window_output)
            try:
                entries.extend(
                    _validate_glossary_window(window_output, window)
                )
            except TranslationWorkflowError as exc:
                return RunError(exc.code, str(exc))
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
    ) -> BlocksResult | Paused | RunError:
        _validate_language_binding(language, source, target_language)
        _validate_glossary_binding(glossary, source, target_language)
        all_blocks = source_blocks(source)
        try:
            blocks = _select_blocks(all_blocks, block_ids)
        except TranslationWorkflowError as exc:
            return RunError(exc.code, str(exc))
        artifact_id = f"{artifact_prefix}/result"
        existing = context.artifacts.find(artifact_id)
        if existing is not None:
            result = BlocksResult.from_document(
                _read_json_artifact(context, existing, "blocks result")
            )
            _validate_blocks_binding(
                result, source, language, target_language, blocks
            )
            return result
        if language.mode == "skipped":
            result = BlocksResult(
                document_digest=source.document_digest,
                source_digest=source.source_digest,
                source_language=language.language_tag,
                target_language=target_language,
                mode="skipped",
                translations=(),
            )
            context.artifacts.publish_json(artifact_id, result.to_document())
            return result
        try:
            windows = _translation_windows(
                blocks,
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
            draft_ref = context.artifacts.find(draft_id)
            if draft_ref is not None:
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
                outcome = _execute(
                    self.task_service,
                    context,
                    request,
                    resume_input=resume_input,
                    options=execution,
                )
                if isinstance(outcome, LLMPaused):
                    return Paused(_awaiting(outcome))
                if isinstance(outcome, LLMFailed):
                    return _run_error(outcome)
                if isinstance(outcome, LLMStopped):
                    raise StoppedError("block translation stopped")
                assert isinstance(outcome, LLMCompleted)
                draft_doc = _object(outcome.value, "translation draft")
                try:
                    _validate_draft_window(draft_doc, window)
                except TranslationWorkflowError as exc:
                    return RunError(exc.code, str(exc))
                context.artifacts.publish_json(draft_id, draft_doc)
            try:
                draft = _validate_draft_window(draft_doc, window)
            except TranslationWorkflowError as exc:
                return RunError(exc.code, str(exc))
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
            review_outcome = _execute(
                self.task_service,
                context,
                review_request,
                resume_input=resume_input,
                options=execution,
            )
            if isinstance(review_outcome, LLMPaused):
                return Paused(_awaiting(review_outcome))
            if isinstance(review_outcome, LLMStopped):
                raise StoppedError("translation review stopped")
            review_error: tuple[str, str] | None = None
            reviewed: list[dict[str, str]] | None = None
            if isinstance(review_outcome, LLMFailed):
                review_error = (
                    review_outcome.error.code.value,
                    str(review_outcome.error),
                )
            else:
                assert isinstance(review_outcome, LLMCompleted)
                try:
                    reviewed = _apply_review(
                        review_outcome.value, draft, window
                    )
                except TranslationWorkflowError as exc:
                    review_error = (exc.code, str(exc))
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
        result = BlocksResult(
            document_digest=source.document_digest,
            source_digest=source.source_digest,
            source_language=language.language_tag,
            target_language=target_language,
            mode="enabled",
            translations=tuple(translations),
        )
        _validate_complete_coverage(result, blocks)
        context.artifacts.publish_json(artifact_id, result.to_document())
        return result


class TranslationWorkflowError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
        _require_fields(entry, _GLOSSARY_ENTRY_FIELDS, "glossary entry")
        if any(entry[field] != term[field] for field in _TERM_IDENTITY_FIELDS):
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
        output.append(dict(entry))  # type: ignore[arg-type]
    return output


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


def _window_glossary(
    blocks: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    text = "\n".join(block_text(item) for item in blocks).casefold()
    return tuple(
        item
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
            {"block_id", "text", "source_identity"},
            "translated block",
        )
        if translated["source_identity"] != source_identity(block):
            raise TranslationWorkflowError(
                "translation_source_identity_invalid",
                f"translation changed source identity for {block['block_id']}",
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


def _validate_blocks_binding(
    result: BlocksResult,
    source: TranslationSource,
    language: LanguageResult,
    target_language: str,
    blocks: Sequence[Mapping[str, Any]],
) -> None:
    if (
        result.document_digest != source.document_digest
        or result.source_digest != source.source_digest
        or result.source_language != language.language_tag
        or result.target_language != target_language
        or result.mode != language.mode
    ):
        raise TranslationWorkflowError(
            "blocks_result_binding_mismatch",
            "blocks result does not match source, language, or target",
        )
    if result.mode == "skipped":
        if result.translations:
            raise TranslationWorkflowError(
                "translation_coverage_invalid",
                "skipped translation result must be empty",
            )
        return
    _validate_complete_coverage(result, blocks)


def _validate_complete_coverage(
    result: BlocksResult, blocks: Sequence[Mapping[str, Any]]
) -> None:
    expected = [str(item["block_id"]) for item in blocks]
    actual = [str(item["block_id"]) for item in result.translations]
    if actual != expected:
        raise TranslationWorkflowError(
            "translation_coverage_invalid",
            "translation result must cover all source blocks in order",
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
    "BlocksResult",
    "GlossaryResult",
    "KeywordProvider",
    "LanguageResult",
    "REVIEW_SUPERVISION_SCHEMA",
    "TranslationWorkflowError",
    "TranslationWorkflowService",
    "outer_resume_input",
]
