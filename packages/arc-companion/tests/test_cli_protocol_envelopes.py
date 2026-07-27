from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from arc_jobs import (
    CommandResult,
    CommandStatus,
    RunSnapshot,
    RunStatus,
)
from arc_paper import (
    RichDocumentParserService,
    RichDocumentValidationError,
    SourceFormat,
    SourceOrigin,
    SourceOriginKind,
    SourceRepository,
)

import arc_companion.cli as cli_module
from arc_companion.cli import main
from arc_companion.project import CompanionProjectPaths
from arc_companion.renderer import CompanionRenderError
from arc_companion.service import CompanionServiceError
from arc_companion.translation_adapter import (
    CompanionTranslationRuntimeError,
)


def _document(tmp_path: Path):
    repository = SourceRepository(tmp_path / "paper")
    artifact = repository.store_bytes(
        b"# Source\n\nBody.\n",
        source_format=SourceFormat.MARKDOWN,
        origin=SourceOrigin(
            SourceOriginKind.LOCAL_IMPORT,
            locator="source.md",
        ),
    )
    return RichDocumentParserService(repository).parse_source(artifact)


def _result(capsys) -> dict:
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == "arc.command_result.v2"
    return result


def test_main_emits_protocol_envelopes_for_build_resume_render_and_validate(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    document = _document(tmp_path)
    project = tmp_path / "project"
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nBody.\n", encoding="utf-8")
    snapshots = [object(), object()]
    calls: list[tuple[str, object]] = []

    class FakeService:
        def __init__(self, _repository) -> None:
            pass

        def prepare(self, request, **kwargs):
            calls.append(("prepare", request))
            assert kwargs["run_id"] == "companion-fake"
            assert CompanionProjectPaths.load(project).current_run_id is None
            return SimpleNamespace(run_id=kwargs["run_id"])

        def execute(self, run_id, **kwargs):
            calls.append(("execute", run_id))
            assert run_id == "companion-fake"
            assert (
                CompanionProjectPaths.load(project).current_run_id
                == "companion-fake"
            )
            assert kwargs["execution"].workers == 3
            return snapshots[0]

        def resume(self, run_id, **kwargs):
            calls.append(("resume", kwargs["input"]))
            assert run_id == "companion-fake"
            assert kwargs["execution"].workers == 2
            return snapshots[1]

        def inspect(self, run_id):
            assert run_id == "companion-fake"
            return SimpleNamespace(
                snapshot=SimpleNamespace(status=RunStatus.SUCCEEDED)
            )

        def accepted_book(self, run_id):
            assert run_id == "companion-fake"
            return object()

    class FakePublisher:
        def publish(self, _book, *, run_id):
            assert run_id == "companion-fake"
            return SimpleNamespace(
                release_id="release-fake",
                reused=False,
                available_formats=("pdf", "web"),
                warnings=(),
                manifest=project
                / "releases"
                / "release-fake"
                / "manifest.json",
                pdf=project
                / "releases"
                / "release-fake"
                / "companion.pdf",
                web_index=(
                    project
                    / "releases"
                    / "release-fake"
                    / "reader"
                    / "index.html"
                ),
            )

        def validate(self, release_id, _book):
            assert release_id == "release-fake"
            return self.publish(_book, run_id="companion-fake")

        def validate_current(self, pointer, _book):
            assert pointer == paths.current_release()
            return self.validate(pointer["release_id"], _book)

    def fake_snapshot_result(_paths, snapshot, **_kwargs):
        operation = "build" if snapshot is snapshots[0] else "resume"
        return CommandResult(
            CommandStatus.COMPLETED,
            data={"operation": operation},
        )

    monkeypatch.setattr(cli_module, "CompanionService", FakeService)
    monkeypatch.setattr(
        cli_module,
        "_resolve_source",
        lambda *_args, **_kwargs: (document, (), ()),
    )
    monkeypatch.setattr(
        cli_module,
        "companion_run_id",
        lambda *_args: "companion-fake",
    )
    monkeypatch.setattr(cli_module, "_snapshot_result", fake_snapshot_result)
    monkeypatch.setattr(
        cli_module,
        "_publisher",
        lambda _paths, **_kwargs: FakePublisher(),
    )

    assert main(
        [
            "build",
            str(source),
            "--project-dir",
            str(project),
            "--workers",
            "3",
        ]
    ) == 0
    assert _result(capsys)["data"] == {"operation": "build"}

    assert main(
        [
            "resume",
            "--project-dir",
            str(project),
            "--workers",
            "2",
            "--input",
            '{"resume_key":"review","action":"discard_review"}',
        ]
    ) == 0
    assert _result(capsys)["data"] == {"operation": "resume"}
    assert calls[-1] == (
        "resume",
        {"resume_key": "review", "action": "discard_review"},
    )

    assert main(
        ["render", "--project-dir", str(project), "--format", "all"]
    ) == 0
    rendered = _result(capsys)
    assert rendered["data"] == {
        "release_id": "release-fake",
        "reused": False,
        "available_formats": ["pdf", "web"],
        "delivery": {
            "html": str(project / "companion.html"),
            "pdf": str(project / "companion.pdf"),
        },
    }
    assert {item["role"] for item in rendered["artifacts"]} == {
        "manifest",
        "pdf",
        "web",
    }
    artifacts = {item["role"]: item["path"] for item in rendered["artifacts"]}
    assert artifacts["pdf"] == str(project / "companion.pdf")
    assert artifacts["web"] == str(project / "companion.html")
    assert artifacts["manifest"] == str(
        project / "releases" / "release-fake" / "manifest.json"
    )

    paths = CompanionProjectPaths.load(project)
    paths.publish_current(
        release_id="release-fake",
        manifest=project / "releases" / "release-fake" / "manifest.json",
        run_id="companion-fake",
    )
    assert main(["validate", "--project-dir", str(project)]) == 0
    validated = _result(capsys)
    assert validated["data"] == {
        "release_id": "release-fake",
        "valid": True,
        "available_formats": ["pdf", "web"],
        "delivery": {
            "html": str(project / "companion.html"),
            "pdf": str(project / "companion.pdf"),
        },
    }
    assert {item["role"] for item in validated["artifacts"]} == {
        "manifest",
        "pdf",
        "web",
    }


def test_render_reports_web_only_release_without_a_pdf_path(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    project.select_run("companion-web-only")

    class FakeService:
        def __init__(self, _repository) -> None:
            pass

        def inspect(self, _run_id):
            return SimpleNamespace(
                snapshot=SimpleNamespace(status=RunStatus.SUCCEEDED)
            )

        def accepted_book(self, _run_id):
            return object()

    class FakePublisher:
        def publish(self, _book, *, run_id):
            assert run_id == "companion-web-only"
            return SimpleNamespace(
                release_id="release-web-only",
                reused=False,
                available_formats=("web",),
                warnings=("XeLaTeX compilation failed",),
                manifest=(
                    project.releases_root
                    / "release-web-only"
                    / "manifest.json"
                ),
                pdf=None,
                web_index=(
                    project.releases_root
                    / "release-web-only"
                    / "reader"
                    / "index.html"
                ),
            )

    monkeypatch.setattr(cli_module, "CompanionService", FakeService)
    monkeypatch.setattr(
        cli_module,
        "_publisher",
        lambda _paths, **_kwargs: FakePublisher(),
    )

    assert main(
        ["render", "--project-dir", str(project.root), "--format", "all"]
    ) == 0
    result = _result(capsys)

    assert result["status"] == "completed"
    assert result["data"]["available_formats"] == ["web"]
    assert result["data"]["delivery"] == {
        "html": str(project.delivery_html)
    }
    assert {item["role"] for item in result["artifacts"]} == {
        "manifest",
        "web",
    }
    assert result["warnings"] == [
        {
            "code": "pdf_render_failed",
            "message": "XeLaTeX compilation failed",
            "details": {
                "format": "pdf",
                "available_formats": ["web"],
            },
        }
    ]


def test_web_render_failure_does_not_change_successful_build_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    snapshot = RunSnapshot(
        run_id="companion-accepted",
        revision=3,
        status=RunStatus.SUCCEEDED,
        attempt=1,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
    )

    class FakeService:
        def __init__(self, _repository) -> None:
            pass

        def accepted_book(self, run_id):
            assert run_id == snapshot.run_id
            return object()

        def translation_reuse_receipt(self, run_id):
            assert run_id == snapshot.run_id
            return None

        def build_diagnostics(self, run_id):
            assert run_id == snapshot.run_id
            return None

    class FailingPublisher:
        def publish(self, _book, *, run_id):
            assert run_id == snapshot.run_id
            raise CompanionRenderError("Web reader validation failed")

    monkeypatch.setattr(cli_module, "CompanionService", FakeService)
    monkeypatch.setattr(
        cli_module,
        "_publisher",
        lambda _paths, **_kwargs: FailingPublisher(),
    )

    result = cli_module._snapshot_result(project, snapshot)

    assert result.status is CommandStatus.COMPLETED
    assert result.data["run"]["status"] == "succeeded"
    assert result.data["published"] is False
    assert result.data["available_formats"] == []
    assert result.artifacts == ()
    assert result.warnings == (
        cli_module.CommandWarning(
            "web_render_failed",
            "Web reader validation failed",
            {"format": "web", "available_formats": []},
        ),
    )


def test_main_model_provider_errors_use_invalid_request_envelope(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nBody.\n", encoding="utf-8")

    assert main(
        [
            "build",
            str(source),
            "--project-dir",
            str(tmp_path / "model-project"),
            "--model",
            "exact-model",
        ]
    ) == 1
    model = _result(capsys)
    assert model["status"] == "failed"
    assert model["error"]["code"] == "invalid_request"

    assert main(
        [
            "build",
            str(source),
            "--project-dir",
            str(tmp_path / "provider-project"),
            "--provider",
            "",
        ]
    ) == 1
    provider = _result(capsys)
    assert provider["status"] == "failed"
    assert provider["error"]["code"] == "invalid_request"

    assert main(
        [
            "build",
            str(source),
            "--project-dir",
            str(tmp_path / "language-project"),
            "--target-language",
            "   ",
        ]
    ) == 1
    language = _result(capsys)
    assert language["status"] == "failed"
    assert language["error"]["code"] == "invalid_request"

    assert not (tmp_path / "model-project").exists()
    assert not (tmp_path / "provider-project").exists()
    assert not (tmp_path / "language-project").exists()


def test_main_render_errors_use_typed_failure_envelope(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "_dispatch",
        lambda _args: (_ for _ in ()).throw(
            CompanionRenderError("render validation failed")
        ),
    )

    assert main(
        ["status", "--project-dir", str(tmp_path / "project")]
    ) == 1
    result = _result(capsys)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "render_failed"


def test_build_preflights_translation_before_creating_project(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nBody.\n", encoding="utf-8")
    project = tmp_path / "project"

    def missing_runtime() -> None:
        raise CompanionTranslationRuntimeError("missing translation runtime")

    monkeypatch.setattr(
        cli_module,
        "require_translation_runtime",
        missing_runtime,
    )

    assert main(
        ["build", str(source), "--project-dir", str(project)]
    ) == 1

    result = _result(capsys)
    assert result["error"]["code"] == "runtime_dependency_missing"
    assert not project.exists()


def test_resume_preflights_translation_without_mutating_project(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    project = CompanionProjectPaths.open(tmp_path / "project")
    project.select_run("companion-existing")
    before = {
        path.relative_to(project.root): path.read_bytes()
        for path in project.root.rglob("*")
        if path.is_file()
    }

    def missing_runtime() -> None:
        raise CompanionTranslationRuntimeError("missing translation runtime")

    monkeypatch.setattr(
        cli_module,
        "require_translation_runtime",
        missing_runtime,
    )

    assert main(
        ["resume", "--project-dir", str(project.root)]
    ) == 1

    result = _result(capsys)
    assert result["error"]["code"] == "runtime_dependency_missing"
    after = {
        path.relative_to(project.root): path.read_bytes()
        for path in project.root.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            CompanionServiceError("accepted_book_invalid", "bad artifact"),
            "accepted_book_invalid",
        ),
        (OSError("disk unavailable"), "local_io_error"),
        (RuntimeError("unexpected failure"), "internal_error"),
    ],
)
def test_main_classifies_typed_io_and_internal_failures(
    error: Exception,
    code: str,
    capsys,
    monkeypatch,
) -> None:
    def fail(_args):
        raise error

    monkeypatch.setattr(cli_module, "_dispatch", fail)

    assert main(["status", "--project-dir", "unused"]) == 1
    result = _result(capsys)
    assert result["status"] == "failed"
    assert result["error"] == {
        "code": code,
        "message": str(error),
        "details": {},
    }


def test_main_propagates_all_structured_pdf_conflicts(capsys, monkeypatch) -> None:
    conflicts = (
        {
            "subject_id": "section:first",
            "status": "missing",
            "page_candidates": [],
        },
        {
            "subject_id": "section:second",
            "status": "ambiguous",
            "page_candidates": [1, 2],
        },
    )

    def fail(_args):
        raise RichDocumentValidationError(
            "pdf_validator_ambiguous",
            "PDF validator ambiguous for 2 source subjects",
            details=conflicts,
        )

    monkeypatch.setattr(cli_module, "_dispatch", fail)

    assert main(["build", "source", "--project-dir", "unused"]) == 1
    result = _result(capsys)
    assert result["error"]["code"] == "pdf_validator_ambiguous"
    assert result["error"]["details"] == {"conflicts": list(conflicts)}
