from __future__ import annotations

from types import SimpleNamespace

from arc_jobs import RunContext, RunError, RunRepository, RunSpec, Succeeded
from arc_llm import LLMExecutionOptions, ModelSelection
from arc_proposer_reviewer import (
    BatchResult,
    LoopResult,
    LoopTermination,
)
from arc_proposer_reviewer.protocol import encode_batch_result

import arc_companion.build as build_module
from arc_companion.editorial_review import (
    editorial_proposal_digest,
    editorial_unit_content_digest,
)
from arc_companion.request_contracts import CompanionGenerationRecipe


def _context(tmp_path) -> RunContext:
    repository = RunRepository(tmp_path / "jobs")
    snapshot = repository.create(
        RunSpec("editorial-run", "handler", {"input": "fixture"})
    )
    return RunContext(repository, snapshot, resume_input=None)


def _unit(unit_id: str, chapter_id: str, body: str) -> dict:
    return {
        "unit_id": unit_id,
        "title": f"Title {unit_id}",
        "anchor_block_ids": [f"block-{chapter_id}"],
        "purpose": "companion",
        "content_markdown": body,
        "citations": [],
    }


def _fixture(context: RunContext):
    source_chapters = (
        SimpleNamespace(chapter_id="chapter-a"),
        SimpleNamespace(chapter_id="chapter-b"),
    )
    accepted = (
        {
            "chapter_id": "chapter-a",
            "title": "Chapter A",
            "learning_units": [
                _unit("unit-a", "chapter-a", "Repeated setup.")
            ],
        },
        {
            "chapter_id": "chapter-b",
            "title": "Chapter B",
            "learning_units": [
                _unit("unit-b", "chapter-b", "Repeated setup with payoff.")
            ],
        },
    )
    for chapter in source_chapters:
        context.artifacts.publish_json(
            f"chapters/{chapter.chapter_id}/guide-accepted",
            {
                "references": [
                    {
                        "reference_id": (
                            "ref-a" if chapter.chapter_id == "chapter-a" else "ref-b"
                        )
                    }
                ]
            },
        )
    return source_chapters, accepted


def _handler() -> SimpleNamespace:
    return SimpleNamespace(
        recipe=CompanionGenerationRecipe(
            model=ModelSelection(provider="test", model="fixture"),
            cross_chapter_editorial_review=True,
        ),
        request=SimpleNamespace(user_intent="Keep local explanations useful."),
        task_service=object(),
        llm_options=LLMExecutionOptions(),
    )


def _source_inputs(context: RunContext):
    ref = context.artifacts.publish_json(
        "source/model-index", {"chapters": ["chapter-a", "chapter-b"]}
    )
    return (build_module._llm_input(context, "companion-source-index", ref),)


def test_scoped_editorial_batch_applies_only_reviewed_edit(
    tmp_path, monkeypatch
) -> None:
    context = _context(tmp_path)
    source_chapters, accepted = _fixture(context)
    calls = []

    class FakeService:
        def __init__(self, task_service):
            assert task_service is not None

        def execute(self, run_context, request, *, options, execution_scope=None):
            calls.append((request, options, execution_scope))
            inventory_digest = request.loops[0].context["inventory_digest"]
            proposal = {
                "inventory_digest": inventory_digest,
                "findings": [
                    {
                        "finding_id": "finding-overlap",
                        "unit_ids": ["unit-a", "unit-b"],
                        "redundancy_assessment": "The setup is repeated.",
                        "retained_value_analysis": "The payoff remains local.",
                        "edits": [
                            {
                                "edit_id": "edit-a",
                                "unit_id": "unit-a",
                                "base_content_digest": editorial_unit_content_digest(
                                    accepted[0]["learning_units"][0]
                                ),
                                "action": "revise",
                                "title": "Chapter-specific setup",
                                "markdown_body": "Setup needed only for Chapter A.",
                            },
                            {
                                "edit_id": "edit-b",
                                "unit_id": "unit-b",
                                "base_content_digest": editorial_unit_content_digest(
                                    accepted[1]["learning_units"][0]
                                ),
                                "action": "omit",
                            },
                        ],
                    }
                ],
            }
            review = {
                "schema_version": "arc.proposer_reviewer.review.v1",
                "action": "stop",
                "reason": "Revise the setup but retain the local payoff.",
                "feedback": {"editorial-proposer": "Audit complete."},
                "payload": {
                    "inventory_digest": inventory_digest,
                    "proposal_digest": editorial_proposal_digest(proposal),
                    "checked_source_anchors": True,
                    "checked_user_intent": True,
                    "checked_frozen_references": True,
                    "approved_edit_ids": ["edit-a"],
                    "rejected_edits": [
                        {"edit_id": "edit-b", "reason": "Locally necessary."}
                    ],
                },
            }
            result = BatchResult(
                "arc.proposer_reviewer.result.v1",
                (
                    LoopResult(
                        "cross-chapter-editorial",
                        LoopTermination.REVIEWER_STOP,
                        1,
                        {"editorial-proposer": proposal},
                        review,
                        None,
                    ),
                ),
            )
            ref = run_context.artifacts.publish_json(
                "fake/editorial-result", encode_batch_result(result)
            )
            return Succeeded(ref)

    monkeypatch.setattr(build_module, "ProposerReviewerService", FakeService)
    resolved, report = build_module.CompanionBuildHandler._cross_chapter_editorial_review(
        _handler(),
        context,
        source_chapters,
        accepted,
        source_inputs=_source_inputs(context),
    )

    assert len(calls) == 1
    request, options, scope = calls[0]
    assert scope == "companion-editorial"
    assert options.max_concurrent_loops == options.max_concurrent_workers == 1
    assert {item.input_id for item in request.inputs} == {
        "companion-source-index",
        "companion-editorial-index",
        "companion-editorial-full-text",
    }
    assert request.loops[0].max_rounds == 3
    assert request.loops[0].review_final_round is True
    assert request.loops[0].context["reference_ids"] == ["ref-a", "ref-b"]
    assert request.loops[0].context["input_manifest"]["source_evidence"] == [
        "companion-source-index"
    ]
    assert resolved[0]["learning_units"][0]["title"] == (
        "Chapter-specific setup"
    )
    assert resolved[1]["learning_units"][0]["unit_id"] == "unit-b"
    assert report["status"] == "applied"
    assert report["counts"]["rejected_edits"] == 1
    assert context.artifacts.find("editorial/resolved-guides") is not None
    assert context.artifacts.find("editorial/report") is not None
    assert context.artifacts.find("chapters/chapter-a/guide-accepted") is not None


def test_editorial_model_failure_preserves_guides_and_publishes_warning(
    tmp_path, monkeypatch
) -> None:
    context = _context(tmp_path)
    source_chapters, accepted = _fixture(context)

    class FailedService:
        def __init__(self, task_service):
            pass

        def execute(self, run_context, request, *, options, execution_scope=None):
            result = BatchResult(
                "arc.proposer_reviewer.result.v1",
                (
                    LoopResult(
                        "cross-chapter-editorial",
                        LoopTermination.FAILED,
                        0,
                        {},
                        None,
                        RunError("provider_failed", "provider unavailable"),
                    ),
                ),
            )
            return Succeeded(
                run_context.artifacts.publish_json(
                    "fake/editorial-failed", encode_batch_result(result)
                )
            )

    monkeypatch.setattr(build_module, "ProposerReviewerService", FailedService)
    resolved, report = build_module.CompanionBuildHandler._cross_chapter_editorial_review(
        _handler(),
        context,
        source_chapters,
        accepted,
        source_inputs=_source_inputs(context),
    )

    assert resolved == accepted
    assert report["status"] == "unavailable"
    assert report["warnings"] == ["provider_failed: provider unavailable"]


def test_editorial_not_applicable_never_calls_model(tmp_path, monkeypatch) -> None:
    context = _context(tmp_path)
    source_chapters, accepted = _fixture(context)

    class UnexpectedService:
        def __init__(self, task_service):
            raise AssertionError("model service must not be constructed")

    monkeypatch.setattr(build_module, "ProposerReviewerService", UnexpectedService)
    resolved, report = build_module.CompanionBuildHandler._cross_chapter_editorial_review(
        _handler(),
        context,
        source_chapters[:1],
        accepted[:1],
        source_inputs=_source_inputs(context),
    )

    assert resolved == accepted[:1]
    assert report["status"] == "not_applicable"
