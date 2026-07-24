"""Closed request contracts for durable domain builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, cast

from arc_jobs import ArtifactRef, decode_artifact_ref, encode_artifact_ref
from arc_llm import InvalidRequestError, ModelSelection
from arc_paper import normalize_paper_id


DOMAIN_BUILD_POLICY_SCHEMA_VERSION = "arc.domain_build_policy.v1"
DOMAIN_BUILD_REQUEST_SCHEMA_VERSION = "arc.domain_build_request.v1"
DOMAIN_BUILD_RESULT_SCHEMA_VERSION = "arc.domain_build_result.v1"


@dataclass(frozen=True)
class DomainBuildPolicy:
    """Fully resolved, bounded selection policy for one domain build."""

    as_of_date: str
    recent_window_days: int = 365
    citer_pool_limit: int = 1000
    ranked_paper_limit: int = 50
    graph_node_limit: int = 90

    def __post_init__(self) -> None:
        _validate_iso_date(self.as_of_date)
        _validate_positive_int("recent_window_days", self.recent_window_days)
        _validate_bounded_int("citer_pool_limit", self.citer_pool_limit, minimum=1, maximum=1000)
        _validate_positive_int("ranked_paper_limit", self.ranked_paper_limit)
        _validate_bounded_int("graph_node_limit", self.graph_node_limit, minimum=2)
        if self.graph_node_limit <= self.ranked_paper_limit:
            raise ValueError("graph_node_limit must be greater than ranked_paper_limit.")


@dataclass(frozen=True)
class DomainBuildRequest:
    """Normalized semantic input for the ``arc.domain.build.v1`` handler."""

    seed_paper: str
    intent: str
    policy: DomainBuildPolicy
    model: ModelSelection = field(default_factory=ModelSelection)

    def __post_init__(self) -> None:
        if not isinstance(self.seed_paper, str):
            raise ValueError("seed_paper must be a string.")
        normalized = normalize_paper_id(self.seed_paper)
        if not normalized:
            raise ValueError("seed_paper must not be empty.")
        if not _is_supported_paper_id(normalized):
            raise ValueError(
                "seed_paper must be a supported arXiv, DOI, or INSPIRE identifier."
            )
        if not isinstance(self.intent, str):
            raise ValueError("intent must be a string.")
        if not isinstance(self.policy, DomainBuildPolicy):
            raise ValueError("policy must be a DomainBuildPolicy.")
        if not isinstance(self.model, ModelSelection):
            raise ValueError("model must be a ModelSelection.")
        object.__setattr__(self, "seed_paper", normalized)
        object.__setattr__(self, "intent", self.intent.strip())


@dataclass(frozen=True)
class DomainBuildWarning:
    """One non-fatal issue retained in a completed build result."""

    code: str
    message: str
    stage: str
    paper_id: str | None = None

    def __post_init__(self) -> None:
        _validate_nonempty_string("warning.code", self.code)
        _validate_nonempty_string("warning.message", self.message)
        _validate_nonempty_string("warning.stage", self.stage)
        if self.paper_id is not None:
            _validate_nonempty_string("warning.paper_id", self.paper_id)


@dataclass(frozen=True)
class DomainBuildResult:
    """Artifact references and warnings produced by one successful build."""

    domain_id: str
    foundation_selection: ArtifactRef
    graph: ArtifactRef
    network_html: ArtifactRef
    paper_json_pack: ArtifactRef
    evidence_pack: ArtifactRef
    summary: ArtifactRef | None
    summary_markdown: ArtifactRef | None
    warnings: tuple[DomainBuildWarning, ...] = ()

    def __post_init__(self) -> None:
        _validate_nonempty_string("domain_id", self.domain_id)
        for field_name in (
            "foundation_selection",
            "graph",
            "network_html",
            "paper_json_pack",
            "evidence_pack",
        ):
            _validate_artifact_ref(field_name, getattr(self, field_name))
        for field_name in ("summary", "summary_markdown"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_artifact_ref(field_name, value)
        if isinstance(self.warnings, (str, bytes)):
            raise ValueError("warnings must be a sequence of DomainBuildWarning values.")
        try:
            warnings = tuple(self.warnings)
        except TypeError as exc:
            raise ValueError(
                "warnings must be a sequence of DomainBuildWarning values."
            ) from exc
        if any(not isinstance(item, DomainBuildWarning) for item in warnings):
            raise ValueError("warnings must contain only DomainBuildWarning values.")
        object.__setattr__(self, "warnings", warnings)


def encode_domain_build_policy(policy: DomainBuildPolicy) -> dict[str, Any]:
    """Encode a fully resolved policy as its exact versioned document."""

    if not isinstance(policy, DomainBuildPolicy):
        raise ValueError("policy must be a DomainBuildPolicy.")
    return {
        "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
        "as_of_date": policy.as_of_date,
        "recent_window_days": policy.recent_window_days,
        "citer_pool_limit": policy.citer_pool_limit,
        "ranked_paper_limit": policy.ranked_paper_limit,
        "graph_node_limit": policy.graph_node_limit,
    }


def decode_domain_build_policy(document: Mapping[str, Any]) -> DomainBuildPolicy:
    """Decode an exact, fully resolved policy document."""

    value = _exact_object(
        document,
        {
            "schema_version",
            "as_of_date",
            "recent_window_days",
            "citer_pool_limit",
            "ranked_paper_limit",
            "graph_node_limit",
        },
        "policy",
    )
    if value["schema_version"] != DOMAIN_BUILD_POLICY_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {DOMAIN_BUILD_POLICY_SCHEMA_VERSION}.")
    return DomainBuildPolicy(
        as_of_date=_required_string(value, "as_of_date", "policy"),
        recent_window_days=_required_int(value, "recent_window_days", "policy"),
        citer_pool_limit=_required_int(value, "citer_pool_limit", "policy"),
        ranked_paper_limit=_required_int(value, "ranked_paper_limit", "policy"),
        graph_node_limit=_required_int(value, "graph_node_limit", "policy"),
    )


def encode_domain_build_request(request: DomainBuildRequest) -> dict[str, Any]:
    """Encode a domain-build request as its exact versioned document."""

    if not isinstance(request, DomainBuildRequest):
        raise ValueError("request must be a DomainBuildRequest.")
    return {
        "schema_version": DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
        "seed_paper": request.seed_paper,
        "intent": request.intent,
        "policy": encode_domain_build_policy(request.policy),
        "model": _model_to_document(request.model),
    }


def decode_domain_build_request(document: Mapping[str, Any]) -> DomainBuildRequest:
    """Decode an exact build request and normalize its seed and intent."""

    value = _exact_object(
        document,
        {"schema_version", "seed_paper", "intent", "policy", "model"},
        "request",
    )
    if value["schema_version"] != DOMAIN_BUILD_REQUEST_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {DOMAIN_BUILD_REQUEST_SCHEMA_VERSION}.")
    return DomainBuildRequest(
        seed_paper=_required_string(value, "seed_paper", "request"),
        intent=_required_string(value, "intent", "request"),
        policy=decode_domain_build_policy(_object(value["policy"], "request.policy")),
        model=_model_from_document(_object(value["model"], "request.model")),
    )


def encode_domain_build_warning(warning: DomainBuildWarning) -> dict[str, Any]:
    """Encode one warning as its exact unversioned nested shape."""

    if not isinstance(warning, DomainBuildWarning):
        raise ValueError("warning must be a DomainBuildWarning.")
    return {
        "code": warning.code,
        "message": warning.message,
        "stage": warning.stage,
        "paper_id": warning.paper_id,
    }


def decode_domain_build_warning(document: Mapping[str, Any]) -> DomainBuildWarning:
    """Decode one exact warning document."""

    value = _exact_object(
        document,
        {"code", "message", "stage", "paper_id"},
        "warning",
    )
    paper_id = value["paper_id"]
    if paper_id is not None and not isinstance(paper_id, str):
        raise ValueError("warning.paper_id must be a string or null.")
    return DomainBuildWarning(
        code=_required_string(value, "code", "warning"),
        message=_required_string(value, "message", "warning"),
        stage=_required_string(value, "stage", "warning"),
        paper_id=paper_id,
    )


def encode_domain_build_result(result: DomainBuildResult) -> dict[str, Any]:
    """Encode a completed build result as its exact versioned document."""

    if not isinstance(result, DomainBuildResult):
        raise ValueError("result must be a DomainBuildResult.")
    return {
        "schema_version": DOMAIN_BUILD_RESULT_SCHEMA_VERSION,
        "domain_id": result.domain_id,
        "foundation_selection": encode_artifact_ref(result.foundation_selection),
        "graph": encode_artifact_ref(result.graph),
        "network_html": encode_artifact_ref(result.network_html),
        "paper_json_pack": encode_artifact_ref(result.paper_json_pack),
        "evidence_pack": encode_artifact_ref(result.evidence_pack),
        "summary": _encode_nullable_artifact_ref(result.summary),
        "summary_markdown": _encode_nullable_artifact_ref(result.summary_markdown),
        "warnings": [encode_domain_build_warning(item) for item in result.warnings],
    }


def decode_domain_build_result(document: Mapping[str, Any]) -> DomainBuildResult:
    """Decode an exact build result using the shared artifact-ref codec."""

    value = _exact_object(
        document,
        {
            "schema_version",
            "domain_id",
            "foundation_selection",
            "graph",
            "network_html",
            "paper_json_pack",
            "evidence_pack",
            "summary",
            "summary_markdown",
            "warnings",
        },
        "result",
    )
    if value["schema_version"] != DOMAIN_BUILD_RESULT_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {DOMAIN_BUILD_RESULT_SCHEMA_VERSION}.")
    raw_warnings = value["warnings"]
    if not isinstance(raw_warnings, list):
        raise ValueError("result.warnings must be an array.")
    return DomainBuildResult(
        domain_id=_required_string(value, "domain_id", "result"),
        foundation_selection=decode_artifact_ref(value["foundation_selection"]),
        graph=decode_artifact_ref(value["graph"]),
        network_html=decode_artifact_ref(value["network_html"]),
        paper_json_pack=decode_artifact_ref(value["paper_json_pack"]),
        evidence_pack=decode_artifact_ref(value["evidence_pack"]),
        summary=_decode_nullable_artifact_ref(value["summary"], "result.summary"),
        summary_markdown=_decode_nullable_artifact_ref(
            value["summary_markdown"], "result.summary_markdown"
        ),
        warnings=tuple(
            decode_domain_build_warning(_object(item, f"result.warnings[{index}]"))
            for index, item in enumerate(raw_warnings)
        ),
    )


def _model_to_document(model: ModelSelection) -> dict[str, Any]:
    return {
        "provider": model.provider,
        "model": model.model,
        "tier": model.tier,
    }


def _model_from_document(document: Mapping[str, Any]) -> ModelSelection:
    value = _exact_object(document, {"provider", "model", "tier"}, "request.model")
    provider = _required_string(value, "provider", "request.model")
    exact_model = value["model"]
    if exact_model is not None and not isinstance(exact_model, str):
        raise ValueError("request.model.model must be a string or null.")
    tier = _required_string(value, "tier", "request.model")
    try:
        return ModelSelection(provider=provider, model=exact_model, tier=cast(Any, tier))
    except InvalidRequestError as exc:
        raise ValueError(f"request.model is invalid: {exc}") from exc


def _is_supported_paper_id(normalized: str) -> bool:
    prefix, separator, value = normalized.partition(":")
    if not separator or not value:
        return False
    if prefix == "arXiv":
        candidate = value
    elif prefix == "doi":
        candidate = value
    elif prefix == "inspire":
        candidate = f"recid:{value}"
    else:
        return False
    return normalize_paper_id(candidate) == normalized


def _validate_artifact_ref(field_name: str, value: object) -> None:
    if not isinstance(value, ArtifactRef):
        raise ValueError(f"{field_name} must be an ArtifactRef.")
    try:
        encode_artifact_ref(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ArtifactRef: {exc}") from exc


def _encode_nullable_artifact_ref(value: ArtifactRef | None) -> dict[str, Any] | None:
    return None if value is None else encode_artifact_ref(value)


def _decode_nullable_artifact_ref(value: object, field_name: str) -> ArtifactRef | None:
    if value is None:
        return None
    try:
        return decode_artifact_ref(cast(Any, value))
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ArtifactRef: {exc}") from exc


def _validate_nonempty_string(field_name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string.")


def _validate_iso_date(value: object) -> None:
    if not isinstance(value, str):
        raise ValueError("as_of_date must be an ISO date string.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as_of_date must be an ISO date string.") from exc
    if parsed.isoformat() != value:
        raise ValueError("as_of_date must be a canonical ISO date string.")


def _validate_positive_int(field_name: str, value: object) -> None:
    _validate_bounded_int(field_name, value, minimum=1)


def _validate_bounded_int(
    field_name: str,
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer.")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{field_name} must be at least {minimum}.")
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")


def _exact_object(
    value: object,
    expected_fields: set[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    actual_fields = set(value)
    if actual_fields != expected_fields:
        missing = sorted(expected_fields - actual_fields)
        extra = sorted(str(item) for item in actual_fields - expected_fields)
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if extra:
            details.append(f"unknown fields: {', '.join(extra)}")
        raise ValueError(f"{name} must have an exact shape ({'; '.join(details)}).")
    return value


def _object(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object.")
    return value


def _required_string(value: Mapping[str, Any], field_name: str, name: str) -> str:
    field = value[field_name]
    if not isinstance(field, str):
        raise ValueError(f"{name}.{field_name} must be a string.")
    return field


def _required_int(value: Mapping[str, Any], field_name: str, name: str) -> int:
    field = value[field_name]
    if isinstance(field, bool) or not isinstance(field, int):
        raise ValueError(f"{name}.{field_name} must be an integer.")
    return field
