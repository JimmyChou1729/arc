from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/arc/skills/arc/scripts"


def delivery_module():
    spec = importlib.util.spec_from_file_location("report_dependencies_fixture", SCRIPTS / "_arc_workflows/report_delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_tools_and_unknown_fonts_are_not_reported_available(monkeypatch):
    module = delivery_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    report = module.report_dependencies(main_font="Fixture", cjk_font="Fixture")
    assert report["status"] == "unavailable"
    assert report["fonts"]["mainfont"]["status"] == "unknown"
    monkeypatch.setenv("ARC_REPORT_MAIN_FONT", "")
    assert module.report_dependencies()["status"] == "invalid_configuration"


def test_font_match_requires_exact_family_and_render_uses_configuration(tmp_path, monkeypatch):
    module = delivery_module()
    monkeypatch.setattr(module.shutil, "which", lambda name: "/fixture/bin/" + name)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="Fixture Sans\n", stderr=""))
    report = module.report_dependencies(main_font="Fixture Sans", cjk_font="Missing CJK")
    assert report["fonts"]["mainfont"]["status"] == "available"
    assert report["fonts"]["CJKmainfont"]["status"] == "missing"
    source = tmp_path / "report.md"
    source.write_text("# Fixture")
    calls = []
    def render(command, **kwargs):
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_bytes(b"%PDF-1.7\nfixture")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", render)
    monkeypatch.setenv("ARC_REPORT_MAIN_FONT", "Fixture Sans")
    module.render_markdown_pdf(project_dir=tmp_path, source=source, output=tmp_path / "report.pdf", cjk_font="Fixture CJK")
    assert "mainfont=Fixture Sans" in calls[0]
    assert "CJKmainfont=Fixture CJK" in calls[0]
    with pytest.raises(module.ReportDeliveryContractError):
        module.report_fonts("invalid\nfont", "fixture")


def test_arc_doctor_runs_offline_and_does_not_claim_host_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("AC_LLM_HOST_COORDINATOR", json.dumps({"coordinator_id": "offline"}))
    completed = subprocess.run([sys.executable, str(SCRIPTS / "doctor-arc.py"), "--project-dir", str(tmp_path)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["providers"]["host"]["available"] is True
    assert report["environment"]["host"]["runtime_tools_verified"] is False
    assert report["environment"]["write"]["status"] == "available"
    assert "scipy" in report["scientific_libraries"]
