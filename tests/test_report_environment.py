from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/arc/skills/arc/scripts"
sys.path.insert(0, str(SCRIPTS))
environment = importlib.import_module("_arc_workflows.report_environment")
delivery = importlib.import_module("_arc_workflows.report_delivery")
sys.path.remove(str(SCRIPTS))


def test_setup_verifies_sources_and_reuses_without_download(tmp_path, monkeypatch):
    payloads = {name: name.encode() for name in ("regular.otf", "bold.otf", "OFL.txt")}
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"upstream_commit": "fixture", "files": [
        {"name": name, "url": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in payloads.items()]}))
    monkeypatch.setattr(environment, "FONT_LOCK", lock)
    monkeypatch.setattr(environment.shutil, "which", lambda name: sys.executable)
    destination = tmp_path / "profile"
    result = environment.setup_report_environment(destination, fetch=payloads.__getitem__)
    assert result["status"] == "ready" and result["reused"] is False
    def forbidden(url):
        raise AssertionError("valid setup must reuse verified assets")
    assert environment.setup_report_environment(destination, fetch=forbidden)["reused"] is True
    config_path = destination / "report-environment.json"
    config = json.loads(config_path.read_text())
    config["tools"]["unexpected"] = 42
    config_path.write_text(json.dumps(config))
    assert delivery.report_dependencies(environment=config_path)["status"] == "invalid_configuration"
    del config["tools"]["unexpected"]
    config_path.write_text(json.dumps(config))
    (destination / "regular.otf").write_bytes(b"corrupt")
    with pytest.raises(environment.ReportEnvironmentError, match="integrity"):
        environment.setup_report_environment(destination, fetch=forbidden)
    assert (destination / "regular.otf").read_bytes() == b"corrupt"


def test_failed_download_does_not_publish_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(environment.shutil, "which", lambda name: sys.executable)
    destination = tmp_path / "profile"
    with pytest.raises(environment.ReportEnvironmentError, match="verification"):
        environment.setup_report_environment(destination, fetch=lambda url: b"incorrect")
    assert not destination.exists()


@pytest.mark.parametrize("missing", ["xeCJK.sty", "lmodern.sty"])
def test_missing_tex_preserves_last_successful_report(tmp_path, monkeypatch, missing):
    source = tmp_path / "source.md"
    source.write_text("# Source")
    output = tmp_path / "report.pdf"
    output.write_bytes(b"previous verified report")
    monkeypatch.setattr(delivery, "report_dependencies", lambda **kwargs: {
        "status": "tex_packages_missing", "tex_packages": {missing: False}, "fonts": {}, "guidance": "explicit setup required"})
    with pytest.raises(delivery.ReportDeliveryUnavailable, match=missing) as error:
        delivery.render_markdown_pdf(project_dir=tmp_path, source=source, output=output)
    assert error.value.code == "pdf_tex_packages_missing"
    assert output.read_bytes() == b"previous verified report"


def test_coverage_requires_chinese_and_exact_font_match():
    assert delivery._covers_chinese("20-7e 4e00-9fff")
    assert not delivery._covers_chinese("20-7e")


def test_diagnostic_detects_missing_distribution_template_font(monkeypatch):
    monkeypatch.setattr(delivery.shutil, "which", lambda *args, **kwargs: "/fixture/tool")
    def probe(command, env):
        if command[0] == "kpsewhich":
            return None if command[1] == "lmodern.sty" else "/fixture/tex.sty"
        return "Noto Sans CJK SC\n20-7e 4e00-9fff"
    monkeypatch.setattr(delivery, "_probe", probe)
    diagnostic = delivery.report_dependencies()
    assert diagnostic["status"] == "tex_packages_missing"
    assert diagnostic["tex_packages"]["lmodern.sty"] is False
