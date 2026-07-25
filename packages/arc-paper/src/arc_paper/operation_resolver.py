"""Bounded, reusable resolution of registry-backed paper operations.

The resolver owns package mechanics only: registry codecs, service reuse,
admission accounting, and path-free provenance.  Callers remain responsible
for choosing an operation allowlist and a request budget appropriate to their
workflow.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .ids import normalize_paper_id
from .registry import OperationSpec, get_operation
from .service import ArcPaperService


_SERVICE_METHODS: Mapping[str, str] = {
    "get-title": "get_title",
    "get-abstract": "get_abstract",
    "get-authors": "get_authors",
    "get-metadata": "get_metadata",
    "get-citer-count": "get_citer_count",
    "get-references": "get_references",
    "get-citers": "get_citers",
    "search-metadata": "search_metadata",
    "search-cached-full-text": "search_cached_full_text",
    "get-arxiv-table-of-contents": "get_arxiv_table_of_contents",
    "get-arxiv-section": "get_arxiv_section",
    "search-arxiv-full-text": "search_arxiv_full_text",
    "search-arxiv-equations": "search_arxiv_equations",
}


@dataclass(frozen=True)
class PaperOperationProvenance:
    """Canonical, public provenance for one resolver admission."""

    operation_id: str
    parameters: Mapping[str, Any]
    canonical_arxiv_id: str
    source_digest: str
    document_digest: str
    request_number: int

    def to_document(self) -> dict[str, Any]:
        return {
            "source": "arc-paper",
            "operation_id": self.operation_id,
            "parameters": dict(self.parameters),
            "canonical_arxiv_id": self.canonical_arxiv_id,
            "source_digest": self.source_digest,
            "document_digest": self.document_digest,
            "request_number": self.request_number,
        }


@dataclass(frozen=True)
class PaperOperationFailure:
    """Stable failure information returned without workflow-specific wording."""

    code: str
    message: str

    def to_document(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class PaperOperationResult:
    """Closed success-or-error result for one operation request."""

    provenance: PaperOperationProvenance
    data: Any = None
    error: PaperOperationFailure | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def operation_id(self) -> str:
        return self.provenance.operation_id

    @property
    def parameters(self) -> Mapping[str, Any]:
        return self.provenance.parameters

    def to_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "ok": self.ok,
            "operation_id": self.operation_id,
            "parameters": dict(self.parameters),
            "provenance": self.provenance.to_document(),
        }
        if self.error is None:
            document["data"] = self.data
        else:
            document["error"] = self.error.to_document()
        return document


@dataclass(frozen=True)
class PaperOperationRecord:
    """Path-free audit projection retained by a resolver instance."""

    request_id: str | None
    result: PaperOperationResult

    def to_document(self) -> dict[str, Any]:
        document = {
            "ok": self.result.ok,
            **self.result.provenance.to_document(),
        }
        if self.request_id is not None:
            document["request_id"] = self.request_id
        if self.result.error is not None:
            document["error"] = self.result.error.to_document()
        return document


class PaperOperationResolver:
    """Resolve an explicit subset of service-backed registry operations.

    One service instance is reused for the lifetime of the resolver so its
    in-memory document cache remains effective.  Calls are serialized because
    an :class:`ArcPaperService` instance is not documented as thread-safe;
    admissions and record snapshots remain safe for concurrent callers.
    """

    def __init__(
        self,
        *,
        allowed_operations: Iterable[str],
        request_limit: int,
        service: ArcPaperService | None = None,
    ) -> None:
        if (
            isinstance(request_limit, bool)
            or not isinstance(request_limit, int)
            or request_limit < 1
        ):
            raise ValueError("request_limit must be a positive integer")
        if isinstance(allowed_operations, (str, bytes)):
            raise ValueError("allowed_operations must be an iterable of operation names")

        specs: dict[str, OperationSpec[Any]] = {}
        allowed_tokens: dict[str, OperationSpec[Any]] = {}
        for operation in allowed_operations:
            if not isinstance(operation, str):
                raise ValueError("allowed operation names must be strings")
            spec = get_operation(operation)
            if spec is None:
                raise ValueError(f"unknown arc-paper operation: {operation}")
            if spec.name not in _SERVICE_METHODS:
                raise ValueError(
                    "operation is not supported by the reusable paper service: "
                    f"{spec.name}"
                )
            specs.setdefault(spec.name, spec)
            allowed_tokens.setdefault(operation, spec)
        if not specs:
            raise ValueError("allowed_operations must not be empty")

        self.request_limit = request_limit
        self.service = service or ArcPaperService()
        self._specs = specs
        self._allowed_tokens = allowed_tokens
        self._request_count = 0
        self._records: list[PaperOperationRecord] = []
        self._state_lock = threading.Lock()
        self._service_lock = threading.Lock()

    @property
    def operation_specs(self) -> tuple[OperationSpec[Any], ...]:
        return tuple(self._specs.values())

    @property
    def request_count(self) -> int:
        with self._state_lock:
            return self._request_count

    @property
    def records(self) -> tuple[PaperOperationRecord, ...]:
        with self._state_lock:
            return tuple(self._records)

    def resolve(
        self,
        operation: str,
        parameters: Mapping[str, Any],
        *,
        request_id: str | None = None,
    ) -> PaperOperationResult:
        with self._state_lock:
            self._request_count += 1
            request_number = self._request_count

        spec = get_operation(operation)
        allowed_spec = self._allowed_tokens.get(operation)
        normalized = _normalize_parameters(parameters)
        if allowed_spec is None:
            operation_id = spec.operation_id if spec is not None else str(operation)
            return self._record_failure(
                request_id=request_id,
                operation_id=operation_id,
                parameters=normalized,
                request_number=request_number,
                code="operation_not_allowed",
                message=f"operation is not allowed by this resolver: {operation}",
            )
        if request_number > self.request_limit:
            return self._record_failure(
                request_id=request_id,
                operation_id=allowed_spec.operation_id,
                parameters=normalized,
                request_number=request_number,
                code="request_limit_exceeded",
                message=(
                    "paper operation request limit exceeded: "
                    f"{self.request_limit}"
                ),
            )

        try:
            decoded = allowed_spec.input_codec.decode(normalized)
            method = getattr(self.service, _SERVICE_METHODS[allowed_spec.name])
            with self._service_lock:
                data = allowed_spec.output_codec.encode(method(**decoded))
        except Exception as exc:
            return self._record_failure(
                request_id=request_id,
                operation_id=allowed_spec.operation_id,
                parameters=normalized,
                request_number=request_number,
                code=str(getattr(exc, "code", "operation_failed")),
                message=str(exc) or type(exc).__name__,
            )

        provenance = _provenance(
            operation_id=allowed_spec.operation_id,
            parameters=normalized,
            data=data,
            request_number=request_number,
        )
        result = PaperOperationResult(provenance=provenance, data=data)
        self._append_record(PaperOperationRecord(request_id, result))
        return result

    def _record_failure(
        self,
        *,
        request_id: str | None,
        operation_id: str,
        parameters: Mapping[str, Any],
        request_number: int,
        code: str,
        message: str,
    ) -> PaperOperationResult:
        result = PaperOperationResult(
            provenance=_provenance(
                operation_id=operation_id,
                parameters=parameters,
                data=None,
                request_number=request_number,
            ),
            error=PaperOperationFailure(code=code, message=message),
        )
        self._append_record(PaperOperationRecord(request_id, result))
        return result

    def _append_record(self, record: PaperOperationRecord) -> None:
        with self._state_lock:
            self._records.append(record)


def _normalize_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    normalized_parameters = dict(parameters)
    for key in ("arxiv_id", "paper_id"):
        value = normalized_parameters.get(key)
        if isinstance(value, str):
            normalized = normalize_paper_id(value)
            if normalized:
                normalized_parameters[key] = normalized
    return normalized_parameters


def _provenance(
    *,
    operation_id: str,
    parameters: Mapping[str, Any],
    data: Any,
    request_number: int,
) -> PaperOperationProvenance:
    canonical_arxiv_id = ""
    for key in ("arxiv_id", "paper_id"):
        value = parameters.get(key)
        if isinstance(value, str):
            normalized = normalize_paper_id(value)
            if normalized.startswith("arXiv:"):
                canonical_arxiv_id = normalized
                break

    source_digest = ""
    document_digest = ""
    if isinstance(data, Mapping):
        raw_provenance = data.get("provenance")
        if isinstance(raw_provenance, Mapping):
            canonical_arxiv_id = str(
                raw_provenance.get("canonical_arxiv_id") or canonical_arxiv_id
            )
            source_digest = str(raw_provenance.get("source_digest") or "")
            document_digest = str(raw_provenance.get("document_digest") or "")

    return PaperOperationProvenance(
        operation_id=operation_id,
        parameters=MappingProxyType(dict(parameters)),
        canonical_arxiv_id=canonical_arxiv_id,
        source_digest=source_digest,
        document_digest=document_digest,
        request_number=request_number,
    )


__all__ = [
    "PaperOperationFailure",
    "PaperOperationProvenance",
    "PaperOperationRecord",
    "PaperOperationResolver",
    "PaperOperationResult",
]
