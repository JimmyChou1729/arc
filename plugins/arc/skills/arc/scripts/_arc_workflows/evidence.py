"""Bounded controller-side paper evidence for ARC Skill worker interactions."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

from arc_llm import (
    InteractionRequest,
    InteractionResponse,
    OperationContract,
)
from arc_paper import (
    ArcPaperService,
    get_operation,
    normalize_paper_id,
)


EVIDENCE_REQUEST_LIMIT = 24
EVIDENCE_OPERATION_NAMES = (
    "get-metadata",
    "get-references",
    "get-citers",
    "search-metadata",
    "get-arxiv-table-of-contents",
    "get-arxiv-section",
    "search-arxiv-full-text",
    "search-arxiv-equations",
)

_SERVICE_METHODS = {
    "get-metadata": "get_metadata",
    "get-references": "get_references",
    "get-citers": "get_citers",
    "search-metadata": "search_metadata",
    "get-arxiv-table-of-contents": "get_arxiv_table_of_contents",
    "get-arxiv-section": "get_arxiv_section",
    "search-arxiv-full-text": "search_arxiv_full_text",
    "search-arxiv-equations": "search_arxiv_equations",
}


def evidence_operation_contracts() -> dict[str, OperationContract]:
    contracts: dict[str, OperationContract] = {}
    for name in EVIDENCE_OPERATION_NAMES:
        spec = get_operation(name)
        if spec is None:
            raise RuntimeError(f"arc-paper operation is unavailable: {name}")
        contracts[name] = OperationContract(
            arguments_schema=spec.input_codec.schema,
            response_schema=_response_schema(
                operation_id=spec.operation_id,
                data_schema=spec.output_codec.schema,
            ),
        )
    return contracts


class ArcPaperEvidenceResolver:
    """Resolve only the published paper allowlist within one ideas batch."""

    def __init__(
        self,
        *,
        request_limit: int = EVIDENCE_REQUEST_LIMIT,
        service: ArcPaperService | None = None,
    ) -> None:
        if (
            isinstance(request_limit, bool)
            or not isinstance(request_limit, int)
            or request_limit < 1
        ):
            raise ValueError("request_limit must be a positive integer")
        self.request_limit = request_limit
        self.service = service or ArcPaperService()
        self._request_count = 0
        self._lock = threading.Lock()
        self._service_lock = threading.Lock()
        self.records: list[dict[str, Any]] = []

    @property
    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def resolve(self, request: InteractionRequest) -> InteractionResponse:
        with self._lock:
            self._request_count += 1
            request_number = self._request_count
        normalized = _normalize_parameters(request.operation, request.arguments)
        spec = get_operation(request.operation)
        if spec is None or request.operation not in EVIDENCE_OPERATION_NAMES:
            return self._error(
                request,
                normalized,
                code="evidence_operation_not_allowed",
                message=f"operation is not in the ARC evidence allowlist: {request.operation}",
                request_number=request_number,
            )
        if request_number > self.request_limit:
            return self._error(
                request,
                normalized,
                code="evidence_budget_exhausted",
                message=(
                    f"ideas evidence budget is limited to "
                    f"{self.request_limit} arc-paper requests"
                ),
                request_number=request_number,
            )
        try:
            data = self._invoke(request.operation, normalized)
        except Exception as exc:
            return self._error(
                request,
                normalized,
                code=str(getattr(exc, "code", "evidence_operation_failed")),
                message=str(exc) or type(exc).__name__,
                request_number=request_number,
            )
        provenance = _provenance(
            operation_id=spec.operation_id,
            parameters=normalized,
            data=data,
            request_number=request_number,
        )
        record = {
            "request_id": request.request_id,
            "ok": True,
            **provenance,
        }
        with self._lock:
            self.records.append(record)
        return InteractionResponse(
            request_id=request.request_id,
            result={
                "ok": True,
                "operation_id": spec.operation_id,
                "parameters": normalized,
                "data": data,
                "provenance": provenance,
            },
        )

    def _invoke(self, operation: str, parameters: Mapping[str, Any]) -> Any:
        """Invoke one allowlisted service method through registry codecs.

        The registry remains authoritative for input/output validation while the
        batch keeps one ArcPaperService instance, including its ParsedDocument
        memoization, for all evidence requests.
        """

        spec = get_operation(operation)
        method_name = _SERVICE_METHODS.get(operation)
        if spec is None or method_name is None:
            raise RuntimeError(f"arc-paper operation is unavailable: {operation}")
        decoded = spec.input_codec.decode(parameters)
        method = getattr(self.service, method_name)
        with self._service_lock:
            return spec.output_codec.encode(method(**decoded))

    def _error(
        self,
        request: InteractionRequest,
        parameters: Mapping[str, Any],
        *,
        code: str,
        message: str,
        request_number: int,
    ) -> InteractionResponse:
        spec = get_operation(request.operation)
        operation_id = (
            spec.operation_id if spec is not None else str(request.operation)
        )
        provenance = _provenance(
            operation_id=operation_id,
            parameters=parameters,
            data=None,
            request_number=request_number,
        )
        record = {
            "request_id": request.request_id,
            "ok": False,
            **provenance,
            "error": {"code": code, "message": message},
        }
        with self._lock:
            self.records.append(record)
        return InteractionResponse(
            request_id=request.request_id,
            error={
                "code": code,
                "message": message,
                "operation_id": operation_id,
                "parameters": dict(parameters),
                "provenance": provenance,
            },
        )


def _normalize_parameters(
    operation: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    parameters = dict(arguments)
    identifier_key = (
        "arxiv_id"
        if operation.startswith(("get-arxiv-", "search-arxiv-"))
        else "paper_id"
    )
    identifier = parameters.get(identifier_key)
    if isinstance(identifier, str):
        normalized = normalize_paper_id(identifier)
        if normalized:
            parameters[identifier_key] = normalized
    return parameters


def _provenance(
    *,
    operation_id: str,
    parameters: Mapping[str, Any],
    data: Any,
    request_number: int,
) -> dict[str, Any]:
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
                raw_provenance.get("canonical_arxiv_id")
                or canonical_arxiv_id
            )
            source_digest = str(raw_provenance.get("source_digest") or "")
            document_digest = str(raw_provenance.get("document_digest") or "")
    return {
        "source": "arc-paper",
        "operation_id": operation_id,
        "parameters": dict(parameters),
        "canonical_arxiv_id": canonical_arxiv_id,
        "source_digest": source_digest,
        "document_digest": document_digest,
        "request_number": request_number,
    }


def _response_schema(
    *,
    operation_id: str,
    data_schema: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ok",
            "operation_id",
            "parameters",
            "data",
            "provenance",
        ],
        "properties": {
            "ok": {"const": True},
            "operation_id": {"const": operation_id},
            "parameters": {"type": "object"},
            "data": dict(data_schema),
            "provenance": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "source",
                    "operation_id",
                    "parameters",
                    "canonical_arxiv_id",
                    "source_digest",
                    "document_digest",
                    "request_number",
                ],
                "properties": {
                    "source": {"const": "arc-paper"},
                    "operation_id": {"const": operation_id},
                    "parameters": {"type": "object"},
                    "canonical_arxiv_id": {"type": "string"},
                    "source_digest": {"type": "string"},
                    "document_digest": {"type": "string"},
                    "request_number": {"type": "integer", "minimum": 1},
                },
            },
        },
    }


__all__ = [
    "ArcPaperEvidenceResolver",
    "EVIDENCE_OPERATION_NAMES",
    "EVIDENCE_REQUEST_LIMIT",
    "evidence_operation_contracts",
]
