from __future__ import annotations

from copy import deepcopy

import pytest

from arc_llm import ModelSelection
from arc_domain.contracts import (
    DOMAIN_BUILD_POLICY_SCHEMA_VERSION,
    DOMAIN_BUILD_REQUEST_SCHEMA_VERSION,
    DomainBuildPolicy,
    DomainBuildRequest,
    decode_domain_build_policy,
    decode_domain_build_request,
    encode_domain_build_policy,
    encode_domain_build_request,
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
