from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/arc/skills/arc/scripts/render-report.py"
RANK_SCRIPT = ROOT / "plugins/arc/skills/arc/scripts/rank-ideas.py"


def _fake_pandoc(bin_dir: Path) -> None:
    executable = bin_dir / "pandoc"
    executable.write_text(
        """#!/usr/bin/env python3
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
output.write_bytes(b"%PDF-1.7\\nARC test report\\n%%EOF\\n")
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _failing_pandoc(bin_dir: Path) -> None:
    executable = bin_dir / "pandoc"
    executable.write_text(
        """#!/usr/bin/env python3
import sys
print("renderer unavailable", file=sys.stderr)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)


def _run(project: Path, source: Path, output: Path, bin_dir: Path):
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-dir",
            str(project),
            "--input",
            str(source),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _load_rank_module():
    spec = importlib.util.spec_from_file_location("rank_ideas_delivery", RANK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(RANK_SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path.remove(str(RANK_SCRIPT.parent))
    return module


def test_markdown_source_in_hidden_state_publishes_visible_pdf(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source = project / ".arc" / "domain" / "summary.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Summary\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_pandoc(bin_dir)

    output = project / "domain" / "summary.pdf"
    completed = _run(project, source, output, bin_dir)

    assert completed.returncode == 0, completed.stderr
    assert output.read_bytes().startswith(b"%PDF-")
    payload = json.loads(completed.stdout)
    assert payload == {
        "artifacts": [str(output.resolve())],
        "delivery_status": "published",
        "format": "pdf",
        "requested_artifacts": [str(output.resolve())],
        "schema_version": "arc.report_delivery.v2",
        "warnings": [],
    }
    assert not tuple((project / ".arc" / "report-render").iterdir())


def test_report_delivery_refuses_hidden_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.md"
    source.write_text("# Report\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_pandoc(bin_dir)

    completed = _run(
        project,
        source,
        project / ".arc" / "delivery.pdf",
        bin_dir,
    )

    assert completed.returncode == 1
    assert "output must use a visible project path" in completed.stderr


def test_report_render_failure_is_a_warning_without_a_pdf(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "report.md"
    source.write_text("# Report\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _failing_pandoc(bin_dir)
    output = project / "report.pdf"

    completed = _run(project, source, output, bin_dir)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert not output.exists()
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "arc.report_delivery.v2"
    assert payload["delivery_status"] == "unavailable"
    assert payload["requested_artifacts"] == [str(output.resolve())]
    assert payload["artifacts"] == []
    assert payload["warnings"][0]["code"] == "pdf_render_unavailable"
    assert "renderer unavailable" in payload["warnings"][0]["message"]
    assert source.read_text(encoding="utf-8") == "# Report\n"


def test_visible_copy_refuses_non_pdf_delivery(tmp_path: Path) -> None:
    old_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(RANK_SCRIPT.parent))
    try:
        from _arc_workflows.report_delivery import publish_visible_copy
    finally:
        sys.dont_write_bytecode = old_dont_write_bytecode
        sys.path.remove(str(RANK_SCRIPT.parent))

    project = tmp_path / "project"
    project.mkdir()
    source = project / ".arc" / "report.md"
    source.parent.mkdir()
    source.write_text("# source\n", encoding="utf-8")

    try:
        publish_visible_copy(
            project_dir=project,
            source=source,
            output=project / "report.md",
        )
    except ValueError as exc:
        assert str(exc) == "visible copied deliveries must use the .pdf suffix"
    else:
        raise AssertionError("a visible Markdown copy must be rejected")


def test_rank_ideas_pdf_publishes_archive_and_latest(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    (project / ".arc" / "ideas").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_pandoc(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    module = _load_rank_module()
    monkeypatch.setattr(
        module,
        "rank_run",
        lambda _root, _run_id, *, mode: {"ranking": [], "mode": mode},
    )
    monkeypatch.setattr(module, "markdown_table", lambda _payload: "# Ideas\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RANK_SCRIPT),
            "--project-dir",
            str(project),
            "--run-id",
            "ideas-1",
            "--format",
            "pdf",
        ],
    )

    module.main()

    archived = project / "ideas" / "ideas-1" / "ranked-ideas.pdf"
    latest = project / "ranked-ideas.pdf"
    assert archived.read_bytes().startswith(b"%PDF-")
    assert latest.read_bytes() == archived.read_bytes()
    delivery = json.loads(capsys.readouterr().out)
    assert delivery["format"] == "pdf"
    assert delivery["mode"] == "formal"
    assert delivery["delivery_status"] == "published"
    assert delivery["schema_version"] == "arc.ideas.delivery.v2"
    assert delivery["artifacts"] == [str(archived), str(latest)]


def test_partial_ideas_pdf_publishes_provisional_archive_and_latest(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    (project / ".arc" / "ideas").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_pandoc(bin_dir)
    monkeypatch.setenv(
        "PATH",
        f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    )
    module = _load_rank_module()
    monkeypatch.setattr(
        module,
        "rank_run",
        lambda _root, _run_id, *, mode: {
            "ranking": [],
            "mode": mode,
            "notice": "NON-FORMAL PROVISIONAL REPORT",
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RANK_SCRIPT),
            "--project-dir",
            str(project),
            "--run-id",
            "ideas-1",
            "--mode",
            "partial",
            "--format",
            "pdf",
        ],
    )

    module.main()

    archived = project / "ideas" / "ideas-1" / "partial-ideas.pdf"
    latest = project / "partial-ideas.pdf"
    assert archived.read_bytes().startswith(b"%PDF-")
    assert latest.read_bytes() == archived.read_bytes()
    hidden = (
        project
        / ".arc"
        / "ideas"
        / "reports"
        / "ideas-1"
        / "partial-ideas.md"
    )
    assert hidden.read_text(encoding="utf-8").startswith(
        "# Partial Ideas — Non-Formal Provisional Report"
    )
    delivery = json.loads(capsys.readouterr().out)
    assert delivery["mode"] == "partial"
    assert delivery["artifacts"] == [str(archived), str(latest)]
