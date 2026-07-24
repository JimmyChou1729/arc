from __future__ import annotations

from copy import deepcopy

import pytest

from arc_jobs import ArtifactDigest, ArtifactRef
from arc_llm import ModelSelection
from arc_domain.contracts import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
    DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
    DOMAIN_BUILD_REQUEST_SCHEMA_VERSION_V2,
    DOMAIN_BUILD_RESULT_SCHEMA_VERSION,
    DomainBuildPolicy,
    DomainBuildRequest,
    DomainBuildResult,
    DomainBuildWarning,
    decode_domain_build_policy,
    decode_domain_build_request,
    decode_domain_build_result,
    decode_domain_build_warning,
    encode_domain_build_policy,
    encode_domain_build_request,
    encode_domain_build_result,
    encode_domain_build_warning,
)


def test_policy_defaults_and_closed_round_trip() -> None:
    policy = DomainBuildPolicy(as_of_date="2026-07-24")

    assert policy.recent_window_days == 365
    assert policy.citer_pool_limit == 1000
    assert policy.ranked_paper_limit == 50
    assert policy.graph_node_limit == 90
    document = encode_domain_build_policy(policy)
    assert document == {
        "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
        "as_of_date": "2026-07-24",
        "recent_window_days": 365,
        "citer_pool_limit": 1000,
        "ranked_paper_limit": 50,
        "graph_node_limit": 90,
    }
    assert decode_domain_build_policy(document) == policy


def test_v2_policy_and_request_are_closed_and_round_trip() -> None:
    policy = DomainBuildPolicy(
        as_of_date="2026-07-24",
        recent_window_days=730,
        citer_pool_limit=100,
        ranked_paper_limit=20,
        graph_node_limit=30,
        schema_version=DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
        foundation_mode="fixed_seed",
        citer_selection_mode="strict_window",
    )
    assert encode_domain_build_policy(policy) == {
        "schema_version": "arc.domain_build_policy.v2",
        "as_of_date": "2026-07-24",
        "recent_window_days": 730,
        "citer_pool_limit": 100,
        "ranked_paper_limit": 20,
        "graph_node_limit": 30,
        "foundation_mode": "fixed_seed",
        "citer_selection_mode": "strict_window",
    }
    request = DomainBuildRequest("2401.00001", "scope", policy)
    assert request.schema_version == DOMAIN_BUILD_REQUEST_SCHEMA_VERSION_V2
    assert encode_domain_build_request(request)["schema_version"] == (
        DOMAIN_BUILD_REQUEST_SCHEMA_VERSION_V2
    )
    assert decode_domain_build_request(encode_domain_build_request(request)) == request


@pytest.mark.parametrize(
    "kwargs",
    [
        {"foundation_mode": "fixed_seed"},
        {
            "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
            "foundation_mode": "unknown",
            "citer_selection_mode": "strict_window",
        },
        {
            "schema_version": DOMAIN_BUILD_POLICY_SCHEMA_VERSION_V2,
            "foundation_mode": "fixed_seed",
            "citer_selection_mode": "unknown",
        },
    ],
)
def test_policy_rejects_incomplete_or_unknown_v2_modes(kwargs) -> None:
    with pytest.raises(ValueError):
        DomainBuildPolicy("2026-07-24", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"as_of_date": "2026-7-24"}, "ISO date"),
        ({"as_of_date": "2026-02-29"}, "ISO date"),
        ({"as_of_date": 20260724}, "ISO date"),
        ({"as_of_date": "2026-07-24", "recent_window_days": True}, "integer"),
        ({"as_of_date": "2026-07-24", "recent_window_days": 0}, "at least 1"),
        ({"as_of_date": "2026-07-24", "citer_pool_limit": 0}, "between 1 and 1000"),
        ({"as_of_date": "2026-07-24", "citer_pool_limit": 1001}, "between 1 and 1000"),
        ({"as_of_date": "2026-07-24", "ranked_paper_limit": 0}, "at least 1"),
        ({"as_of_date": "2026-07-24", "graph_node_limit": 1}, "at least 2"),
        (
            {
                "as_of_date": "2026-07-24",
                "ranked_paper_limit": 2,
                "graph_node_limit": 2,
            },
            "greater than ranked_paper_limit",
        ),
    ],
)
def test_policy_rejects_invalid_values(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        DomainBuildPolicy(**kwargs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(extra=True),
        lambda document: document.__setitem__(1, "not-a-json-field"),
        lambda document: document.pop("as_of_date"),
        lambda document: document.__setitem__("schema_version", "arc.domain_build_policy.v0"),
        lambda document: document.__setitem__("recent_window_days", True),
        lambda document: document.__setitem__("graph_node_limit", 50),
    ],
)
def test_policy_decode_is_closed_and_validated(mutate) -> None:
    document = encode_domain_build_policy(DomainBuildPolicy("2026-07-24"))
    mutate(document)

    with pytest.raises(ValueError):
        decode_domain_build_policy(document)


def test_request_normalizes_seed_strips_intent_and_round_trips() -> None:
    request = DomainBuildRequest(
        seed_paper=" https://arxiv.org/abs/2401.00001v3 ",
        intent="  inflation observables  ",
        policy=DomainBuildPolicy("2026-07-24"),
        model=ModelSelection(provider="codex", tier="high"),
    )

    assert request.seed_paper == "arXiv:2401.00001"
    assert request.intent == "inflation observables"
    document = encode_domain_build_request(request)
    assert document == {
        "schema_version": DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
        "seed_paper": "arXiv:2401.00001",
        "intent": "inflation observables",
        "policy": encode_domain_build_policy(request.policy),
        "model": {"provider": "codex", "model": None, "tier": "high"},
    }
    assert decode_domain_build_request(document) == request


def test_request_decode_normalizes_semantic_strings() -> None:
    document = {
        "schema_version": DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
        "seed_paper": "0911.3380",
        "intent": "  ultraviolet completion ",
        "policy": encode_domain_build_policy(DomainBuildPolicy("2026-07-24")),
        "model": {"provider": "auto", "model": None, "tier": "medium"},
    }

    assert decode_domain_build_request(document) == DomainBuildRequest(
        seed_paper="arXiv:0911.3380",
        intent="ultraviolet completion",
        policy=DomainBuildPolicy("2026-07-24"),
    )


@pytest.mark.parametrize(
    ("seed_paper", "normalized"),
    [
        ("https://arxiv.org/abs/hep-th/0601001v2", "arXiv:hep-th/0601001"),
        ("https://doi.org/10.1007/JHEP01(2010)117.", "doi:10.1007/jhep01(2010)117"),
        ("recid:154280", "inspire:154280"),
    ],
)
def test_request_accepts_only_supported_normalized_seed_kinds(
    seed_paper, normalized
) -> None:
    request = DomainBuildRequest(
        seed_paper,
        "",
        DomainBuildPolicy("2026-07-24"),
    )

    assert request.seed_paper == normalized


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(extra=True),
        lambda document: document.pop("intent"),
        lambda document: document.__setitem__("schema_version", "arc.domain_build_request.v0"),
        lambda document: document.__setitem__("seed_paper", 42),
        lambda document: document.__setitem__("intent", None),
        lambda document: document.__setitem__("policy", []),
        lambda document: document.__setitem__("model", []),
        lambda document: document["model"].update(extra=True),
        lambda document: document["model"].pop("tier"),
        lambda document: document["model"].__setitem__("tier", "maximum"),
        lambda document: document["model"].__setitem__("model", 42),
    ],
)
def test_request_decode_is_closed_and_validated(mutate) -> None:
    request = DomainBuildRequest(
        "2401.00001",
        "intent",
        DomainBuildPolicy("2026-07-24"),
    )
    document = deepcopy(encode_domain_build_request(request))
    mutate(document)

    with pytest.raises(ValueError):
        decode_domain_build_request(document)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seed_paper": "", "intent": "", "policy": DomainBuildPolicy("2026-07-24")},
        {
            "seed_paper": "a paper title",
            "intent": "",
            "policy": DomainBuildPolicy("2026-07-24"),
        },
        {
            "seed_paper": "arXiv:not-an-id",
            "intent": "",
            "policy": DomainBuildPolicy("2026-07-24"),
        },
        {
            "seed_paper": "doi:not-a-doi",
            "intent": "",
            "policy": DomainBuildPolicy("2026-07-24"),
        },
        {
            "seed_paper": "inspire:not-a-recid",
            "intent": "",
            "policy": DomainBuildPolicy("2026-07-24"),
        },
        {"seed_paper": 1, "intent": "", "policy": DomainBuildPolicy("2026-07-24")},
        {"seed_paper": "2401.00001", "intent": None, "policy": DomainBuildPolicy("2026-07-24")},
        {"seed_paper": "2401.00001", "intent": "", "policy": {}},
        {
            "seed_paper": "2401.00001",
            "intent": "",
            "policy": DomainBuildPolicy("2026-07-24"),
            "model": {},
        },
    ],
)
def test_request_constructor_rejects_wrong_types(kwargs) -> None:
    with pytest.raises(ValueError):
        DomainBuildRequest(**kwargs)


def _ref(artifact_id: str, *, size_bytes: int = 12) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        digest=ArtifactDigest("sha256", "a" * 64, size_bytes),
        media_type="application/json",
        relative_path=f"objects/{artifact_id}.json",
    )


def _result() -> DomainBuildResult:
    return DomainBuildResult(
        domain_id="arXiv_2401.00001_deadbeef",
        foundation_selection=_ref("foundation-selection"),
        graph=_ref("graph"),
        network_html=ArtifactRef(
            artifact_id="network-html",
            digest=ArtifactDigest("sha256", "b" * 64, 42),
            media_type="text/html",
            relative_path="objects/network.html",
        ),
        paper_json_pack=_ref("paper-json-pack"),
        evidence_pack=_ref("evidence-pack"),
        summary=_ref("summary"),
        summary_markdown=None,
        warnings=(
            DomainBuildWarning(
                code="paper_unavailable",
                message="One selected paper could not be parsed.",
                stage="paper-pack",
                paper_id="arXiv:2402.00001",
            ),
            DomainBuildWarning(
                code="summary_unavailable",
                message="Summary generation timed out.",
                stage="summary",
            ),
        ),
    )


def test_warning_closed_round_trip() -> None:
    warning = DomainBuildWarning(
        code="paper_unavailable",
        message="Paper parse failed.",
        stage="paper-pack",
        paper_id="arXiv:2402.00001",
    )

    document = encode_domain_build_warning(warning)
    assert document == {
        "code": "paper_unavailable",
        "message": "Paper parse failed.",
        "stage": "paper-pack",
        "paper_id": "arXiv:2402.00001",
    }
    assert decode_domain_build_warning(document) == warning


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "", "message": "message", "stage": "stage"},
        {"code": "code", "message": " ", "stage": "stage"},
        {"code": "code", "message": "message", "stage": ""},
        {"code": "code", "message": "message", "stage": "stage", "paper_id": ""},
        {"code": "code", "message": "message", "stage": "stage", "paper_id": 42},
    ],
)
def test_warning_rejects_empty_or_wrong_values(kwargs) -> None:
    with pytest.raises(ValueError):
        DomainBuildWarning(**kwargs)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(extra=True),
        lambda document: document.pop("message"),
        lambda document: document.__setitem__("paper_id", False),
        lambda document: document.__setitem__("code", " "),
    ],
)
def test_warning_decode_is_closed(mutate) -> None:
    document = encode_domain_build_warning(
        DomainBuildWarning("code", "message", "stage")
    )
    mutate(document)

    with pytest.raises(ValueError):
        decode_domain_build_warning(document)


def test_result_closed_round_trip_with_nullable_summary_refs() -> None:
    result = _result()

    document = encode_domain_build_result(result)
    assert set(document) == {
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
    }
    assert document["schema_version"] == DOMAIN_BUILD_RESULT_SCHEMA_VERSION
    assert document["summary_markdown"] is None
    assert document["summary"]["artifact_id"] == "summary"
    assert document["warnings"] == [
        encode_domain_build_warning(item) for item in result.warnings
    ]
    assert decode_domain_build_result(document) == result


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(extra=True),
        lambda document: document.pop("graph"),
        lambda document: document.__setitem__(
            "schema_version", "arc.domain_build_result.v0"
        ),
        lambda document: document.__setitem__("domain_id", ""),
        lambda document: document.__setitem__("warnings", {}),
        lambda document: document["warnings"][0].update(extra=True),
        lambda document: document.__setitem__("summary", False),
        lambda document: document["graph"].pop("relative_path"),
        lambda document: document["graph"]["digest"].update(extra=True),
        lambda document: document["graph"]["digest"].__setitem__(
            "algorithm", "md5"
        ),
        lambda document: document["graph"]["digest"].__setitem__(
            "value", "not-a-digest"
        ),
        lambda document: document["graph"]["digest"].__setitem__(
            "size_bytes", True
        ),
    ],
)
def test_result_decode_rejects_unknown_and_malformed_artifact_refs(mutate) -> None:
    document = deepcopy(encode_domain_build_result(_result()))
    mutate(document)

    with pytest.raises(ValueError):
        decode_domain_build_result(document)


def test_result_constructor_freezes_warnings_and_validates_artifact_refs() -> None:
    warning = DomainBuildWarning("code", "message", "stage")
    result = _result()
    copied = DomainBuildResult(
        domain_id=result.domain_id,
        foundation_selection=result.foundation_selection,
        graph=result.graph,
        network_html=result.network_html,
        paper_json_pack=result.paper_json_pack,
        evidence_pack=result.evidence_pack,
        summary=None,
        summary_markdown=None,
        warnings=[warning],
    )
    assert copied.warnings == (warning,)

    invalid_ref = ArtifactRef(
        "graph",
        ArtifactDigest("sha256", "a" * 64, True),
        "application/json",
        "objects/graph.json",
    )
    with pytest.raises(ValueError, match="valid ArtifactRef"):
        DomainBuildResult(
            domain_id="domain",
            foundation_selection=result.foundation_selection,
            graph=invalid_ref,
            network_html=result.network_html,
            paper_json_pack=result.paper_json_pack,
            evidence_pack=result.evidence_pack,
            summary=None,
            summary_markdown=None,
        )
