"""Advisory cross-candidate assessment for one committed ARC ideas batch."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from arc_jobs import (
    FileLease,
    ImmutableArtifactStore,
    RunRepository,
    RunStatus,
    canonical_json_bytes,
)
from arc_llm import (
    JsonOutput,
    LLMClient,
    LLMCompleted,
    LLMExecutionOptions,
    LLMFailed,
    LLMPaused,
    LLMRequest,
    LLMStopped,
    LLMTaskService,
    ModelSelection,
)
from arc_proposer_reviewer import (
    BatchInspection,
    BatchTrace,
    inspect_batch,
    read_batch_round,
    read_batch_trace,
)
from jsonschema import Draft202012Validator

from _arc_workflows.workflow_io import read_json_object


ASSESSMENT_SCHEMA_VERSION = "arc.ideas.portfolio_assessment.v1"
ASSESSMENT_ARTIFACT_SCHEMA_VERSION = (
    "arc.ideas.portfolio_assessment_artifact.v1"
)
ASSESSMENT_IDENTITY_SCHEMA_VERSION = (
    "arc.ideas.portfolio_assessment_identity.v1"
)
ASSESSMENT_PROMPT_VERSION = "arc.ideas.portfolio_assessment.prompt.v1"
ASSESSMENT_SCHEMA_FILENAME = "ideas-portfolio-assessment.schema.json"
ASSESSMENT_PROMPT_FILENAME = "ideas-portfolio-assessment.prompt.md"
ASSESSMENT_ARTIFACT_PREFIX = "ideas/portfolio-assessments/v1"

_PROPOSAL_CORE_FIELDS = (
    "title",
    "scientific_route",
    "idea_summary",
    "motivation",
    "domain_roles",
    "target_contribution",
    "transfer_map",
    "bold_hypothesis",
    "translation_assumptions",
    "compatibility_checks",
    "novelty_checks",
    "calculation_plan",
    "expected_deliverable",
    "validation_checks",
    "kill_criteria",
    "risks",
)
_ASSESSMENT_EXCLUDED_FIELDS = frozenset(
    {
        "marks",
        "total_score",
        "rank",
        "qualified",
        "qualification",
        "qualification_reasons",
        "recommended_action",
        "selection",
    }
)


class PortfolioAssessmentRunner(Protocol):
    """Injectable typed LLM execution seam."""

    def __call__(
        self,
        request: LLMRequest,
        run_root: Path,
        *,
        options: LLMExecutionOptions,
    ) -> Any: ...


def generate_portfolio_assessment(
    run_root: str | Path,
    run_id: str,
    *,
    user_intent: str,
    runner: PortfolioAssessmentRunner | None = None,
    task_service: LLMTaskService | None = None,
    llm_options: LLMExecutionOptions = LLMExecutionOptions(),
    inspection: BatchInspection | None = None,
    trace: BatchTrace | None = None,
) -> dict[str, Any]:
    """Generate or reuse one advisory assessment for all succeeded loops."""

    repository = RunRepository(Path(run_root).expanduser().resolve())
    try:
        observed = inspection or inspect_batch(repository, run_id)
        committed_trace = trace or read_batch_trace(repository, run_id)
        compact, identity_rounds = _portfolio_source(
            repository,
            run_id,
            inspection=observed,
            trace=committed_trace,
        )
    except Exception as exc:
        return _status("corrupt", reason=type(exc).__name__)
    if not compact:
        return _status("not_run", reason="no_succeeded_committed_rounds")

    identity = _assessment_identity(
        user_intent=user_intent,
        rounds=identity_rounds,
    )
    input_digest = _identity_digest(identity)
    store = ImmutableArtifactStore(
        repository.run_directory(run_id),
        repository_root=repository.root,
    )
    artifact_id = _assessment_artifact_id(input_digest)
    existing = _load_exact_artifact(
        store,
        artifact_id=artifact_id,
        expected_identity=identity,
        expected_digest=input_digest,
    )
    if existing["status"] == "available":
        return {
            "status": "available",
            "input_digest": input_digest,
            "ref": existing["ref"],
            "reused": True,
        }
    if existing["status"] == "corrupt":
        return existing

    request = LLMRequest(
        task_id=f"ideas-portfolio-{input_digest[:24]}",
        prompt=_assessment_prompt(
            user_intent=user_intent,
            candidates=compact,
        ),
        output=JsonOutput(
            _assessment_output_schema(
                [item["candidate_id"] for item in compact]
            )
        ),
        model=ModelSelection(provider="auto", tier="high"),
    )
    assessment_run_root = (
        repository.run_directory(run_id) / "portfolio-assessment-llm"
    )
    lease = FileLease(
        assessment_run_root / "locks" / f"{input_digest}.lock"
    ).acquire(blocking=True)
    try:
        # Another cooperating process may have published while this caller
        # waited. The content digest is both the serialization key and the
        # public reuse identity.
        existing = _load_exact_artifact(
            store,
            artifact_id=artifact_id,
            expected_identity=identity,
            expected_digest=input_digest,
        )
        if existing["status"] == "available":
            return {
                "status": "available",
                "input_digest": input_digest,
                "ref": existing["ref"],
                "reused": True,
            }
        if existing["status"] == "corrupt":
            return existing
        return _generate_locked(
            request=request,
            assessment_run_root=assessment_run_root,
            input_digest=input_digest,
            identity=identity,
            compact=compact,
            artifact_id=artifact_id,
            store=store,
            runner=runner,
            task_service=task_service,
            llm_options=llm_options,
        )
    finally:
        lease.release()


def _generate_locked(
    *,
    request: LLMRequest,
    assessment_run_root: Path,
    input_digest: str,
    identity: Mapping[str, Any],
    compact: list[dict[str, Any]],
    artifact_id: str,
    store: ImmutableArtifactStore,
    runner: PortfolioAssessmentRunner | None,
    task_service: LLMTaskService | None,
    llm_options: LLMExecutionOptions,
) -> dict[str, Any]:
    try:
        if runner is None:
            outcome = _run_default_assessment(
                request,
                assessment_run_root=assessment_run_root,
                input_digest=input_digest,
                task_service=task_service,
                llm_options=llm_options,
            )
        else:
            generated = runner(
                request,
                assessment_run_root,
                options=llm_options,
            )
            outcome = getattr(generated, "outcome", generated)
    except Exception as exc:
        return _status(
            "failed",
            input_digest=input_digest,
            reason=type(exc).__name__,
        )
    if isinstance(outcome, LLMPaused):
        return _status(
            "paused",
            input_digest=input_digest,
            reason=_enum_value(outcome.reason),
        )
    if isinstance(outcome, LLMFailed):
        return _status(
            "failed",
            input_digest=input_digest,
            reason=_error_code(outcome.error),
        )
    if isinstance(outcome, LLMStopped):
        return _status(
            "stopped",
            input_digest=input_digest,
            reason="stopped",
        )
    if not isinstance(outcome, LLMCompleted):
        return _status(
            "failed",
            input_digest=input_digest,
            reason="unknown_typed_outcome",
        )
    assessment = outcome.value
    validation_error = _assessment_validation_error(
        assessment,
        candidate_ids={item["candidate_id"] for item in compact},
    )
    if validation_error is not None:
        return _status(
            "invalid",
            input_digest=input_digest,
            reason=validation_error,
        )
    document = {
        "schema_version": ASSESSMENT_ARTIFACT_SCHEMA_VERSION,
        "input_digest": input_digest,
        "identity": identity,
        "assessment": copy.deepcopy(dict(assessment)),
    }
    try:
        ref = store.publish_json(artifact_id, document)
    except Exception as exc:
        raced = _load_exact_artifact(
            store,
            artifact_id=artifact_id,
            expected_identity=identity,
            expected_digest=input_digest,
        )
        if raced["status"] == "available":
            return {
                "status": "available",
                "input_digest": input_digest,
                "ref": raced["ref"],
                "reused": True,
            }
        return _status(
            "corrupt",
            input_digest=input_digest,
            reason=type(exc).__name__,
        )
    return {
        "status": "available",
        "input_digest": input_digest,
        "ref": _public_ref(ref),
        "reused": False,
    }


def _run_default_assessment(
    request: LLMRequest,
    *,
    assessment_run_root: Path,
    input_digest: str,
    task_service: LLMTaskService | None,
    llm_options: LLMExecutionOptions,
) -> Any:
    """Execute or recover the one deterministic durable run for this input."""

    durable_run_id = f"portfolio-{input_digest[:24]}"
    client = LLMClient(service=task_service or LLMTaskService())
    repository = RunRepository(assessment_run_root)
    snapshot_path = repository.run_directory(durable_run_id) / "snapshot.json"
    if snapshot_path.exists():
        snapshot = repository.inspect(durable_run_id).snapshot
        if snapshot.status is RunStatus.PAUSED:
            assert snapshot.awaiting is not None
            if snapshot.awaiting.input_required:
                awaiting = snapshot.awaiting
                return LLMPaused(
                    awaiting.reason,
                    awaiting.resume_key,
                    details=awaiting.details,
                    request_ref=awaiting.request_ref,
                    input_required=True,
                    response_contract=awaiting.response_contract,
                )
            return client.resume(
                run_root=assessment_run_root,
                run_id=durable_run_id,
                options=llm_options,
            ).outcome
        if snapshot.status in {RunStatus.FAILED, RunStatus.RUNNING}:
            return client.resume(
                run_root=assessment_run_root,
                run_id=durable_run_id,
                options=llm_options,
            ).outcome
    return client.generate(
        request,
        run_root=assessment_run_root,
        run_id=durable_run_id,
        options=llm_options,
    ).outcome


def load_portfolio_assessment(
    run_root: str | Path,
    ranking_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Load only the assessment that exactly matches a ranked run frontier."""

    run_id = ranking_payload.get("run_id")
    user_intent = ranking_payload.get("user_intent")
    if not isinstance(run_id, str) or not run_id:
        return _status("invalid", reason="ranking_run_id_missing")
    if not isinstance(user_intent, str):
        return _status("invalid", reason="ranking_user_intent_missing")

    repository = RunRepository(Path(run_root).expanduser().resolve())
    try:
        inspection = inspect_batch(repository, run_id)
        trace = read_batch_trace(repository, run_id)
        _compact, identity_rounds = _portfolio_source(
            repository,
            run_id,
            inspection=inspection,
            trace=trace,
        )
        if not identity_rounds:
            return _status("missing", reason="no_succeeded_committed_rounds")
        ranked_rounds = _ranking_identity_rounds(ranking_payload)
        if ranked_rounds is None or ranked_rounds != identity_rounds:
            return _status("mismatch", reason="ranking_frontier_differs")
        identity = _assessment_identity(
            user_intent=user_intent,
            rounds=identity_rounds,
        )
        input_digest = _identity_digest(identity)
        store = ImmutableArtifactStore(
            repository.run_directory(run_id),
            repository_root=repository.root,
        )
        loaded = _load_exact_artifact(
            store,
            artifact_id=_assessment_artifact_id(input_digest),
            expected_identity=identity,
            expected_digest=input_digest,
        )
    except Exception as exc:
        return _status("corrupt", reason=type(exc).__name__)
    if loaded["status"] != "available":
        return loaded
    assessment = loaded.pop("assessment")
    return {
        **loaded,
        "content": assessment,
    }


def _portfolio_source(
    repository: RunRepository,
    run_id: str,
    *,
    inspection: BatchInspection,
    trace: BatchTrace,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    succeeded = sorted(
        loop.loop_id for loop in inspection.loops if loop.lifecycle == "succeeded"
    )
    trace_by_loop = {loop.loop_id: loop for loop in trace.loops}
    compact_by_loop: dict[str, list[dict[str, Any]]] = {}
    identity_rounds: list[dict[str, Any]] = []
    for loop_id in succeeded:
        loop_trace = trace_by_loop.get(loop_id)
        if loop_trace is None:
            raise ValueError("succeeded loop is missing from committed trace")
        for round_ref in sorted(
            loop_trace.rounds,
            key=lambda item: item.round_number,
        ):
            committed = read_batch_round(
                repository,
                run_id,
                loop_id,
                round_ref.round_number,
            )
            compact_by_loop.setdefault(loop_id, []).append(
                _compact_round(committed)
            )
            identity_rounds.append(
                _round_identity(
                    loop_id,
                    round_ref.round_number,
                    proposal_refs=round_ref.proposal_refs,
                    review_ref=round_ref.review_ref,
                )
            )
    compact = [
        {"candidate_id": loop_id, "rounds": compact_by_loop[loop_id]}
        for loop_id in sorted(compact_by_loop)
    ]
    return compact, identity_rounds


def _compact_round(committed: Any) -> dict[str, Any]:
    proposals = []
    for proposer_id in sorted(committed.proposals):
        raw = committed.proposals[proposer_id]
        if not isinstance(raw, Mapping):
            continue
        proposals.append(
            {
                "proposer_id": proposer_id,
                "proposal_core": {
                    field: copy.deepcopy(raw[field])
                    for field in _PROPOSAL_CORE_FIELDS
                    if field in raw
                },
            }
        )
    review = committed.review if isinstance(committed.review, Mapping) else {}
    payload = review.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    assessment = payload.get("idea_assessment")
    assessment_kind = "idea_assessment"
    if not isinstance(assessment, Mapping):
        assessment = payload.get("cross_domain_assessment")
        assessment_kind = "cross_domain_assessment"
    benchmark = payload.get("reviewer_benchmark")
    evidence = payload.get("evidence_checked")
    tool_queries = payload.get("tool_queries_used")
    return {
        "round": committed.round_number,
        "proposals": proposals,
        "reviewer_benchmark": (
            copy.deepcopy(dict(benchmark))
            if isinstance(benchmark, Mapping)
            else {}
        ),
        "reviewer_assessment": {
            "kind": assessment_kind if isinstance(assessment, Mapping) else "none",
            "content": (
                _without_assessment_control_fields(assessment)
                if isinstance(assessment, Mapping)
                else {}
            ),
        },
        "reviewer_evidence": _string_list(evidence),
        "reviewer_tool_queries": _string_list(tool_queries),
        "reviewer_limitations": _reviewer_limitations(payload),
    }


def _without_assessment_control_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): copy.deepcopy(item)
        for key, item in value.items()
        if str(key) not in _ASSESSMENT_EXCLUDED_FIELDS
    }


def _reviewer_limitations(payload: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    for assessment_name, fields in (
        (
            "idea_assessment",
            (
                "blocking_feasibility_failures",
                "manageable_feasibility_risks",
            ),
        ),
        (
            "cross_domain_assessment",
            (
                "blocking_compatibility_failures",
                "manageable_compatibility_risks",
                "critical_concerns",
                "disqualifying_reasons",
            ),
        ),
    ):
        assessment = payload.get(assessment_name)
        if not isinstance(assessment, Mapping):
            continue
        for field in fields:
            result.extend(_string_list(assessment.get(field)))
    return list(dict.fromkeys(result))


def _round_identity(
    loop_id: str,
    round_number: int,
    *,
    proposal_refs: Mapping[str, Any],
    review_ref: Any,
) -> dict[str, Any]:
    return {
        "candidate_id": loop_id,
        "round": round_number,
        "proposals": [
            {
                "proposer_id": proposer_id,
                "sha256": str(proposal_refs[proposer_id].sha256),
            }
            for proposer_id in sorted(proposal_refs)
        ],
        "review_sha256": str(review_ref.sha256),
    }


def _assessment_identity(
    *,
    user_intent: str,
    rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = _load_assessment_prompt_template()
    schema = _load_assessment_schema()
    return {
        "schema_version": ASSESSMENT_IDENTITY_SCHEMA_VERSION,
        "user_intent": user_intent,
        "prompt_contract": {
            "version": ASSESSMENT_PROMPT_VERSION,
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        },
        "output_contract": {
            "version": ASSESSMENT_SCHEMA_VERSION,
            "sha256": hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        },
        "rounds": copy.deepcopy(rounds),
    }


def _identity_digest(identity: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(identity)).hexdigest()


def _assessment_artifact_id(input_digest: str) -> str:
    return f"{ASSESSMENT_ARTIFACT_PREFIX}/{input_digest}/assessment.json"


def _assessment_prompt(
    *,
    user_intent: str,
    candidates: list[dict[str, Any]],
) -> str:
    template = _load_assessment_prompt_template()
    return template.replace(
        "<USER_INTENT_JSON>",
        json.dumps(user_intent, ensure_ascii=False),
    ).replace(
        "<CANDIDATES_JSON>",
        json.dumps(candidates, ensure_ascii=False, sort_keys=True),
    )


def _load_assessment_schema(
    workflow_json_dir: Path | None = None,
) -> dict[str, Any]:
    return read_json_object(
        _workflow_json_dir(workflow_json_dir) / ASSESSMENT_SCHEMA_FILENAME
    )


def _assessment_output_schema(candidate_ids: list[str]) -> dict[str, Any]:
    """Bind model-visible candidate references to this exact batch."""

    schema = copy.deepcopy(_load_assessment_schema())
    properties = schema["properties"]
    finding_ids = properties["cross_candidate_findings"]["items"][
        "properties"
    ]["candidate_ids"]["items"]
    finding_ids.clear()
    finding_ids["enum"] = list(candidate_ids)
    notes = properties["candidate_notes"]
    note_id = notes["items"]["properties"]["candidate_id"]
    note_id.clear()
    note_id["enum"] = list(candidate_ids)
    notes["allOf"] = [
        {
            "contains": {
                "type": "object",
                "required": ["candidate_id"],
                "properties": {"candidate_id": {"const": candidate_id}},
            },
            "minContains": 0,
            "maxContains": 1,
        }
        for candidate_id in candidate_ids
    ]
    return schema


def _load_assessment_prompt_template(
    workflow_json_dir: Path | None = None,
) -> str:
    return (
        _workflow_json_dir(workflow_json_dir) / ASSESSMENT_PROMPT_FILENAME
    ).read_text(encoding="utf-8")


def _workflow_json_dir(workflow_json_dir: Path | None = None) -> Path:
    if workflow_json_dir is not None:
        return Path(workflow_json_dir).expanduser()
    return Path(__file__).resolve().parents[2] / "workflows" / "json"


def _assessment_validation_error(
    value: Any,
    *,
    candidate_ids: set[str],
) -> str | None:
    errors = tuple(
        Draft202012Validator(_load_assessment_schema()).iter_errors(value)
    )
    if errors:
        return "output_schema"
    assert isinstance(value, Mapping)
    referenced = {
        str(candidate_id)
        for finding in value.get("cross_candidate_findings", [])
        if isinstance(finding, Mapping)
        for candidate_id in finding.get("candidate_ids", [])
    }
    notes = [
        str(note.get("candidate_id", ""))
        for note in value.get("candidate_notes", [])
        if isinstance(note, Mapping)
    ]
    if referenced.difference(candidate_ids) or set(notes).difference(candidate_ids):
        return "unknown_candidate_id"
    if len(notes) != len(set(notes)):
        return "duplicate_candidate_note"
    return None


def _load_exact_artifact(
    store: ImmutableArtifactStore,
    *,
    artifact_id: str,
    expected_identity: Mapping[str, Any],
    expected_digest: str,
) -> dict[str, Any]:
    try:
        ref = store.find(artifact_id)
        if ref is None:
            return _status(
                "missing",
                input_digest=expected_digest,
                reason="artifact_missing",
            )
        document = json.loads(store.read_bytes(ref).decode("utf-8"))
        if (
            not isinstance(document, Mapping)
            or set(document)
            != {"schema_version", "input_digest", "identity", "assessment"}
            or document.get("schema_version")
            != ASSESSMENT_ARTIFACT_SCHEMA_VERSION
            or document.get("input_digest") != expected_digest
            or document.get("identity") != expected_identity
        ):
            return _status(
                "corrupt",
                input_digest=expected_digest,
                reason="artifact_contract",
            )
        error = _assessment_validation_error(
            document.get("assessment"),
            candidate_ids={
                str(item.get("candidate_id", ""))
                for item in expected_identity.get("rounds", [])
                if isinstance(item, Mapping)
            },
        )
        if error is not None:
            return _status(
                "corrupt",
                input_digest=expected_digest,
                reason=error,
            )
    except Exception as exc:
        return _status(
            "corrupt",
            input_digest=expected_digest,
            reason=type(exc).__name__,
        )
    return {
        "status": "available",
        "input_digest": expected_digest,
        "ref": _public_ref(ref),
        "assessment": copy.deepcopy(dict(document["assessment"])),
    }


def _ranking_identity_rounds(
    payload: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for collection_name in (
        "candidate_ranking",
        "ranking",
        "unqualified",
        "portfolio_excluded",
    ):
        entries = payload.get(collection_name)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            rounds = entry.get("rounds")
            candidates = rounds if isinstance(rounds, list) else [entry]
            for round_entry in candidates:
                if not isinstance(round_entry, Mapping):
                    continue
                loop_id = round_entry.get("loop_id")
                round_number = round_entry.get("round")
                proposer_ref = round_entry.get("proposer_artifact")
                proposal_refs = round_entry.get("proposal_artifacts")
                review_ref = round_entry.get("review_artifact")
                if (
                    not isinstance(loop_id, str)
                    or not isinstance(round_number, int)
                    or not isinstance(review_ref, Mapping)
                    or not isinstance(review_ref.get("sha256"), str)
                ):
                    continue
                proposals: list[dict[str, str]] = []
                if isinstance(proposal_refs, Mapping):
                    for proposer_id, ref in sorted(
                        proposal_refs.items(),
                        key=lambda item: str(item[0]),
                    ):
                        if not isinstance(ref, Mapping) or not isinstance(
                            ref.get("sha256"), str
                        ):
                            proposals = []
                            break
                        proposals.append(
                            {
                                "proposer_id": str(proposer_id),
                                "sha256": ref["sha256"],
                            }
                        )
                elif isinstance(proposer_ref, Mapping) and isinstance(
                    proposer_ref.get("sha256"), str
                ):
                    proposer_id = round_entry.get("proposer_id")
                    if not isinstance(proposer_id, str) or not proposer_id:
                        proposer_id = "proposer_001"
                    proposals = [
                        {
                            "proposer_id": proposer_id,
                            "sha256": proposer_ref["sha256"],
                        }
                    ]
                if not proposals:
                    continue
                normalized = {
                    "candidate_id": loop_id,
                    "round": round_number,
                    "proposals": proposals,
                    "review_sha256": review_ref["sha256"],
                }
                key = (loop_id, round_number)
                if key in found and found[key] != normalized:
                    return None
                found[key] = normalized
    if not found:
        return None
    return [found[key] for key in sorted(found)]


def _public_ref(ref: Any) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "sha256": ref.digest.value,
        "size_bytes": ref.digest.size_bytes,
        "media_type": ref.media_type,
    }


def _status(
    status: str,
    *,
    input_digest: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "input_digest": input_digest,
        "ref": None,
        "reason": reason,
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    ]


def _enum_value(value: Any) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate)


def _error_code(error: Any) -> str:
    return _enum_value(getattr(error, "code", type(error).__name__))


__all__ = [
    "ASSESSMENT_ARTIFACT_SCHEMA_VERSION",
    "ASSESSMENT_IDENTITY_SCHEMA_VERSION",
    "ASSESSMENT_PROMPT_VERSION",
    "ASSESSMENT_SCHEMA_VERSION",
    "PortfolioAssessmentRunner",
    "generate_portfolio_assessment",
    "load_portfolio_assessment",
]
