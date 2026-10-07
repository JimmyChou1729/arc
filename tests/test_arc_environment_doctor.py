from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/arc/skills/arc/scripts"


def delivery_module():
    spec = importlib.util.spec_from_file_location("report_dependencies_fixture", SCRIPTS / "_arc_workflows/report_delivery.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def test_missing_tools_and_unknown_fonts_are_not_reported_available(monkeypatch):
    module = delivery_module()
    monkeypatch.setattr(module.shutil, "which", lambda name, **kwargs: None)
    report = module.report_dependencies(main_font="Fixture", cjk_font="Fixture")
    assert report["status"] == "tools_missing"
    assert report["fonts"]["mainfont"]["status"] == "unknown"
    monkeypatch.setenv("ARC_REPORT_MAIN_FONT", "")
    assert module.report_dependencies()["status"] == "invalid_configuration"


def test_font_match_requires_exact_family_and_render_uses_configuration(tmp_path, monkeypatch):
    module = delivery_module()
    monkeypatch.setattr(module.shutil, "which", lambda name, **kwargs: "/fixture/bin/" + name)
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
    monkeypatch.setattr(module, "report_dependencies", lambda **kwargs: {"status": "available"})
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
    assert os.path.normpath(report["scientific_python"]["executable"]) == os.path.normpath(sys.executable)
    assert report["scientific_python"]["scope"] == "current_interpreter_only"


def test_uninitialized_export_doctor_gives_public_commands_without_installing(tmp_path):
    import shutil
    scripts = tmp_path / "skill/scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    env = dict(os.environ)
    for name in ("ARC_REQUIRE_REPO_ROOT", "AC_FOUNDATION_REPO_ROOT", "AC_PRODUCT_REPO_ROOT", "PYTHONPATH"):
        env.pop(name, None)
    env['AC_RUNTIME_HOME'] = str(tmp_path / 'never-installed')
    before = set(tmp_path.rglob('*'))
    # -S represents an interpreter without the installed AC distributions.
    result = subprocess.run([sys.executable, '-S', str(scripts / 'doctor-arc.py'), '--project-dir', str(tmp_path)],
                            env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'Traceback' not in result.stderr
    report = json.loads(result.stdout)
    assert report['environment']['code'] == 'runtime_dependencies_missing'
    assert 'ac_jobs' in report['environment']['message']
    commands = report['environment']['commands']
    assert commands['setup'] == ['bash', str(scripts / 'arc-runtime'), 'setup']
    assert commands['workflow_doctor'][-2:] == ['--project-dir', str(tmp_path)]
    assert report['report']['status'] == 'not_checked'
    assert set(tmp_path.rglob('*')) == before
    help_result = subprocess.run([sys.executable, '-S', str(scripts / 'doctor-arc.py'), '--help'],
                                 env=env, capture_output=True, text=True)
    assert help_result.returncode == 0 and '--project-dir' in help_result.stdout
    assert not (tmp_path / 'never-installed').exists()
