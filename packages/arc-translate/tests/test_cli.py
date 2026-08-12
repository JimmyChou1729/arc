from __future__ import annotations

import json

from arc_jobs import RunEngine, RunRepository, RunSpec, Succeeded
from arc_render import Layer, SourceIdentity, write_layer
from arc_translate import cli
from arc_translate.handlers import (
    BLOCKS_HANDLER,
    GLOSSARY_HANDLER,
    LANGUAGE_HANDLER,
)
from arc_translate.project import TranslationProject
from arc_translate.workflow import (
    GlossaryResult,
    LanguageResult,
    TranslationResult,
)


def _result(capsys):
    return json.loads(capsys.readouterr().out)


class _ResultHandler:
    def __init__(self, name, result):
        self.name = name
        self.result = result

    def execute(self, context):
        return Succeeded(
            context.artifacts.publish_json("result", self.result.to_document())
        )


def _selected_result(tmp_path, step, result):
    project = TranslationProject.open(tmp_path / "project")
    handlers = {
        "language": LANGUAGE_HANDLER,
        "glossary": GLOSSARY_HANDLER,
        "blocks": BLOCKS_HANDLER,
    }
    run_id = f"{step}-run"
    repository = RunRepository(project.jobs_root)
    snapshot = RunEngine(repository).execute(
        RunSpec(run_id, handlers[step], {}),
        _ResultHandler(handlers[step], result),
    )
    project.select(step, run_id)
    return project, snapshot


def test_cli_rejects_invalid_language_before_project_creation(tmp_path, capsys):
    project = tmp_path / "project"
    code = cli.main(
        [
            "detect-language",
            "paper.md",
            "--target-language",
            " ",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] == "invalid_request"
    assert not project.exists()


def test_cli_never_implicitly_runs_missing_language_step(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    code = cli.main(
        [
            "build-glossary",
            "paper.md",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] in {
        "project_not_found",
        "project_state_conflict",
    }
    assert tuple(project.iterdir()) == ()


def test_cli_validates_approximate_count_without_writes(tmp_path, capsys):
    project = tmp_path / "project"
    code = cli.main(
        [
            "build-glossary",
            "paper.md",
            "--approx-term-count",
            "0",
            "--project-dir",
            str(project),
        ]
    )
    assert code == 1
    assert _result(capsys)["error"]["code"] == "invalid_request"
    assert not project.exists()


def test_get_result_returns_exact_canonical_language_result(tmp_path, capsys):
    result = LanguageResult(
        document_digest="document-digest",
        source_digest="source-digest",
        language_tag="en",
        classification="known",
        confidence=0.95,
        target_language="fr",
        mode="enabled",
    )
    project, snapshot = _selected_result(tmp_path, "language", result)

    code = cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "language",
        ]
    )

    assert code == 0
    envelope = _result(capsys)
    assert envelope["schema_version"] == "arc.command_result.v2"
    assert envelope["status"] == "completed"
    assert envelope["run"] == {
        "id": snapshot.run_id,
        "revision": snapshot.revision,
    }
    assert envelope["data"] == {
        "step": "language",
        "result": result.to_document(),
    }
    assert envelope["artifacts"] == []
    assert envelope["warnings"] == []
    assert envelope["error"] is None
    assert envelope["resume"] is None


def test_get_result_returns_canonical_glossary_result(tmp_path, capsys):
    result = GlossaryResult(
        document_digest="document-digest",
        source_digest="source-digest",
        target_language="fr",
        approx_count=1,
        inventory_digest="a" * 64,
        entries=(),
    )
    project, snapshot = _selected_result(tmp_path, "glossary", result)

    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "glossary",
        ]
    ) == 0

    envelope = _result(capsys)
    assert envelope["run"]["id"] == snapshot.run_id
    assert envelope["data"] == {
        "step": "glossary",
        "result": result.to_document(),
    }
    assert envelope["artifacts"] == []


def test_get_result_includes_existing_blocks_layer_delivery(tmp_path, capsys):
    source = SourceIdentity(
        "markdown",
        "text/markdown",
        "a" * 64,
        10,
        "b" * 64,
    )
    result = TranslationResult(
        source_language="en",
        target_language="fr",
        mode="skipped",
        coverage="document",
        layer=Layer(source, "arc-translate", ()),
        revision_artifacts=(),
    )
    project, snapshot = _selected_result(tmp_path, "blocks", result)
    write_layer(project.translation_layer, result.layer)

    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "blocks",
        ]
    ) == 0

    envelope = _result(capsys)
    assert envelope["run"]["id"] == snapshot.run_id
    assert envelope["data"] == {
        "step": "blocks",
        "result": result.to_document(),
        "delivery": {
            "layer": str(project.translation_layer),
            "revision_count": 0,
        },
    }
    assert envelope["artifacts"] == [
        {
            "role": "layer",
            "id": snapshot.run_id,
            "path": str(project.translation_layer),
        }
    ]


def test_get_result_omits_blocks_delivery_when_layer_is_absent(tmp_path, capsys):
    source = SourceIdentity(
        "markdown",
        "text/markdown",
        "a" * 64,
        10,
        "b" * 64,
    )
    result = TranslationResult(
        source_language="en",
        target_language="fr",
        mode="skipped",
        coverage="document",
        layer=Layer(source, "arc-translate", ()),
        revision_artifacts=(),
    )
    project, _snapshot = _selected_result(tmp_path, "blocks", result)

    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "blocks",
        ]
    ) == 0

    envelope = _result(capsys)
    assert "delivery" not in envelope["data"]
    assert envelope["artifacts"] == []


def test_get_result_reports_missing_and_unfinished_selection(tmp_path, capsys):
    project = TranslationProject.open(tmp_path / "project")

    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "language",
        ]
    ) == 1
    assert _result(capsys)["error"]["code"] == "run_not_selected"

    repository = RunRepository(project.jobs_root)
    repository.create(RunSpec("pending-language", LANGUAGE_HANDLER, {}))
    project.select("language", "pending-language")
    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "language",
        ]
    ) == 1
    assert _result(capsys)["error"]["code"] == "result_unavailable"


def test_status_reports_pending_selected_run_as_query(tmp_path, capsys):
    project = TranslationProject.open(tmp_path / "project")
    repository = RunRepository(project.jobs_root)
    snapshot = repository.create(RunSpec("pending-language", LANGUAGE_HANDLER, {}))
    project.select("language", snapshot.run_id)

    assert cli.main(
        ["status", "--project-dir", str(project.root)]
    ) == 0

    envelope = _result(capsys)
    assert envelope["status"] == "completed"
    assert envelope["error"] is None
    assert envelope["run"] == {
        "id": snapshot.run_id,
        "revision": snapshot.revision,
    }
    assert envelope["data"]["current_step"] == "language"
    assert envelope["data"]["run"]["status"] == "pending"
    assert envelope["data"]["steps"]["language"]["status"] == "pending"


def test_get_result_rejects_corrupt_step_selection(tmp_path, capsys):
    result = GlossaryResult(
        document_digest="document-digest",
        source_digest="source-digest",
        target_language="fr",
        approx_count=1,
        inventory_digest="a" * 64,
        entries=(),
    )
    project, _snapshot = _selected_result(tmp_path, "glossary", result)
    project.select("language", "glossary-run")

    assert cli.main(
        [
            "get-result",
            "--project-dir",
            str(project.root),
            "--step",
            "language",
        ]
    ) == 1

    envelope = _result(capsys)
    assert envelope["error"]["code"] == "result_invalid"
