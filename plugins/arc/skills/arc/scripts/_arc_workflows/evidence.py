"""Controller-side paper evidence for ARC Skill worker interactions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arc_llm import (
    InteractionRequest,
    InteractionResolver,
    InteractionResponse,
    OperationContract,
    ScopedInteractionLedger,
)
from arc_paper import (
    ArcPaperService,
    PaperOperationResolver,
    PaperOperationResult,
    get_operation,
)


EVIDENCE_OPERATION_NAMES = (
    "get-metadata",
    "get-references",
    "get-citers",
    "search-metadata",
    "get-arxiv-table-of-contents",
    "get-arxiv-section",
    "search-arxiv-full-text",
    "search-arxiv-equations",
    "search-cached-full-text",
)


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
        service: ArcPaperService | None = None,
    ) -> None:
        self._resolver = PaperOperationResolver(
            allowed_operations=EVIDENCE_OPERATION_NAMES,
            service=service,
        )

    @property
    def service(self) -> ArcPaperService:
        return self._resolver.service

    @property
    def request_count(self) -> int:
        return self._resolver.request_count

    @property
    def records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for record in self._resolver.records:
            document = record.to_document()
            if record.result.error is not None:
                code, message = _ideas_error(record.result)
                document["error"] = {"code": code, "message": message}
            records.append(document)
        return records

    def resolve(self, request: InteractionRequest) -> InteractionResponse:
        result = self._resolver.resolve(
            request.operation,
            request.arguments,
            request_id=request.request_id,
        )
        if result.ok:
            return InteractionResponse(
                request_id=request.request_id,
                result=result.to_document(),
            )

        code, message = _ideas_error(result)
        return InteractionResponse(
            request_id=request.request_id,
            error={
                "code": code,
                "message": message,
                "operation_id": result.operation_id,
                "parameters": dict(result.parameters),
                "provenance": result.provenance.to_document(),
            },
        )


class IdeasEvidenceLedger:
    """Observe current-process evidence activity globally and per loop."""

    def __init__(self, resolver: Any, loop_ids: list[str]) -> None:
        self.resolver = resolver
        self._ledger = ScopedInteractionLedger(resolver, loop_ids)

    @property
    def request_count(self) -> int:
        return int(self.resolver.request_count)

    @property
    def records(self) -> list[dict[str, Any]]:
        records = [dict(item) for item in self.resolver.records]
        return sorted(records, key=lambda item: int(item["request_number"]))

    def scoped(self, loop_id: str) -> InteractionResolver:
        return self._ledger.scoped(loop_id)

    def per_loop(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for loop_id, counts in self._ledger.snapshot().items():
            attempted = counts["request_count"]
            errors_by_code = dict(sorted(counts["error_counts"].items()))
            failed = sum(errors_by_code.values())
            result[loop_id] = {
                "attempted": attempted,
                "succeeded": attempted - failed,
                "failed": failed,
                "errors_by_code": errors_by_code,
                "repeated_raw_signature": counts[
                    "repeated_request_count"
                ],
            }
        return result

    def to_document(self) -> dict[str, Any]:
        records = self.records
        errors_by_code: dict[str, int] = {}
        succeeded = 0
        for record in records:
            if record["ok"]:
                succeeded += 1
                continue
            error = record.get("error")
            if isinstance(error, Mapping):
                code = error.get("code")
                if isinstance(code, str) and code:
                    errors_by_code[code] = errors_by_code.get(code, 0) + 1
        failed = len(records) - succeeded
        per_loop = self.per_loop()
        return {
            "observation_scope": "current_process",
            "attempted": self.request_count,
            "succeeded": succeeded,
            "failed": failed,
            "errors_by_code": dict(sorted(errors_by_code.items())),
            "repeated_raw_signature": sum(
                counts["repeated_raw_signature"]
                for counts in per_loop.values()
            ),
            "records": records,
            "per_loop": per_loop,
        }


def _ideas_error(result: PaperOperationResult) -> tuple[str, str]:
    failure = result.error
    if failure is None:
        raise ValueError("successful paper operation has no ideas error")
    if failure.code == "operation_not_allowed":
        operation = failure.message.removeprefix(
            "operation is not allowed by this resolver: "
        )
        return (
            "evidence_operation_not_allowed",
            f"operation is not in the ARC evidence allowlist: {operation}",
        )
    if failure.code == "operation_failed":
        return "evidence_operation_failed", failure.message
    return failure.code, failure.message


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
    "IdeasEvidenceLedger",
    "evidence_operation_contracts",
]
