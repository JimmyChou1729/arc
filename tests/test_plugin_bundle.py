from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("arc_plugin_bundle", ROOT / "scripts/plugin_bundle.py")
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def files():
    result = bundle.read_directory(ROOT / "plugins/arc", for_build=True)
    result["LICENSE"] = (ROOT / "LICENSE").read_bytes()
    return result


def modify(values, path, change):
    value = json.loads(values[path])
    change(value)
    values[path] = json.dumps(value).encode()


@pytest.mark.parametrize("profile", bundle.PROFILES)
def test_deterministic_zip_and_sidecars_match_actual_inventory(tmp_path, profile):
    first = bundle.build(ROOT / "plugins/arc", tmp_path / "first.zip", profile)
    second = bundle.build(ROOT / "plugins/arc", tmp_path / "second.zip", profile)
    assert first["valid"] and second["valid"], first["issues"]
    assert first["sha256"] == second["sha256"]
    assert hashlib.sha256((tmp_path / "first.zip").read_bytes()).hexdigest() == first["sha256"]
    actual = bundle.read_zip(tmp_path / "first.zip")
    assert {entry["path"] for entry in first["files"]} == set(actual)
    assert all(hashlib.sha256(actual[item["path"]]).hexdigest() == item["sha256"] for item in first["files"])
    assert (tmp_path / "first.zip.sha256").read_text().startswith(first["sha256"])
    assert json.loads((tmp_path / "first.zip.manifest.json").read_text())["files"] == first["files"]
    with zipfile.ZipFile(tmp_path / "first.zip") as archive:
        runtime = archive.getinfo("skills/arc/scripts/arc-runtime")
        assert runtime.external_attr >> 16 & 0o111
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
    assert first["public_submission_ready"] is False


@pytest.mark.parametrize("manifest", ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"))
@pytest.mark.parametrize("component", ("apps", "hooks", "mcpServers"))
def test_public_rejects_disabled_declarations_in_every_manifest(manifest, component):
    values = files()
    modify(values, manifest, lambda document: document.update({component: "./private.json"}))
    report = bundle.validate_files(values, "public-skills")
    assert not report["valid"]
    assert any(component in item["message"] for item in report["issues"])


def test_private_binding_preserved_and_public_binding_not_silently_dropped(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    values = files()
    modify(values, "plugin.json", lambda document: document["extensions"]["com.openai"].update(apps="./.app.json"))
    values[".app.json"] = b'{"apps": {"fixture": {"id": "fixture-only"}}}'
    for name, data in values.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    private = bundle.build(source, tmp_path / "private.zip", "private-local")
    assert private["valid"], private["issues"]
    assert ".app.json" in bundle.read_zip(tmp_path / "private.zip")
    public = bundle.build(source, tmp_path / "public.zip", "public-skills")
    assert not public["valid"] and not (tmp_path / "public.zip").exists()


@pytest.mark.parametrize("path", ("../bad", "/absolute", "C:/absolute", "skills//bad", "skills\\bad", " bad", "skills//", "a/" * 20 + "b"))
def test_bad_zip_paths_are_rejected(tmp_path, path):
    target = tmp_path / "bad.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(path, b"fixture")
    with pytest.raises(bundle.BundleError):
        bundle.read_zip(target)


@pytest.mark.parametrize("names", (("A", "a"), ("é", "e\u0301"), ("skills", "skills/arc/SKILL.md"), ("SKILLS", "skills/arc/SKILL.md")))
def test_normalized_and_file_directory_conflicts_are_rejected(tmp_path, names):
    target = tmp_path / "collision.zip"
    with zipfile.ZipFile(target, "w") as archive:
        for name in names:
            archive.writestr(name, b"fixture")
    with pytest.raises(bundle.BundleError):
        bundle.read_zip(target)


def test_symlink_and_size_limits_are_rejected(tmp_path, monkeypatch):
    target = tmp_path / "symlink.zip"
    with zipfile.ZipFile(target, "w") as archive:
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, b"outside")
    with pytest.raises(bundle.BundleError):
        bundle.read_zip(target)
    monkeypatch.setattr(bundle, "MAX_ARCHIVE", 1)
    with pytest.raises(bundle.BundleError, match="100 MB"):
        bundle.read_zip(target)


def test_metadata_schema_icon_and_cache_failures_remain_visible():
    values = files()
    modify(values, "plugin.json", lambda document: document.update(unrecognized=True))
    assert bundle.validate_files(values, "public-skills")["checks"]["schema"] == "failed"
    values = files()
    modify(values, ".codex-plugin/plugin.json", lambda document: document["interface"].update(shortDescription="x" * 31))
    assert not bundle.validate_files(values, "public-skills")["valid"]
    values = files()
    values["assets/logo.svg"] = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 inf inf"/>'
    assert not bundle.validate_files(values, "public-skills")["valid"]
    values = files()
    values["skills/arc/scripts/__pycache__/fixture.pyc"] = b"fixture"
    assert bundle.validate_files(values, "public-skills")["checks"]["project"] == "failed"


def test_malformed_manifest_and_skill_metadata_report_issues():
    values = files()
    values["plugin.json"] = b'{"name":"arc","name":"other"}'
    assert not bundle.validate_files(values, "public-skills")["valid"]
    values = files()
    modify(values, "plugin.json", lambda document: document.update(extensions=[]))
    assert not bundle.validate_files(values, "public-skills")["valid"]
    values = files()
    values["skills/arc/agents/openai.yaml"] = b"interface: [wrong]\n"
    assert not bundle.validate_files(values, "public-skills")["valid"]


def test_shadowed_portable_manifest_and_nested_root_are_validated():
    values = files()
    document = json.loads(values["plugin.json"])
    document["unrecognized"] = True
    values[".agent-plugin/plugin.json"] = json.dumps(document).encode()
    assert bundle.validate_files(values, "public-skills")["checks"]["schema"] == "failed"
    document.pop("$schema")
    values[".agent-plugin/plugin.json"] = json.dumps(document).encode()
    assert bundle.validate_files(values, "public-skills")["checks"]["schema"] == "failed"
    values = files()
    values["skills/arc/references/plugin.json"] = b"{}"
    assert bundle.validate_files(values, "public-skills")["checks"]["project"] == "failed"


def test_original_zip_filename_with_nul_is_rejected(tmp_path):
    path = tmp_path / "nul.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("invalidQZ", b"fixture")
    path.write_bytes(path.read_bytes().replace(b"invalidQZ", b"invalid\x00Z"))
    with pytest.raises(bundle.BundleError, match="truncated control"):
        bundle.read_zip(path)


@pytest.mark.parametrize("profile", bundle.PROFILES)
def test_clean_extraction_doctor_and_full_offline_host_cycle(tmp_path, profile):
    path = tmp_path / "arc.zip"
    assert bundle.build(ROOT / "plugins/arc", path, profile)["valid"]
    extraction = tmp_path / "extracted"
    with zipfile.ZipFile(path) as archive:
        archive.extractall(extraction)
    assert bundle.validate_files(bundle.read_directory(extraction), profile)["valid"]
    env = dict(os.environ, AC_LLM_HOST_COORDINATOR=json.dumps({"coordinator_id": "offline-bundle"}), PYTHONDONTWRITEBYTECODE="1")
    env.pop("ARC_REQUIRE_REPO_ROOT", None)
    doctor = subprocess.run([sys.executable, str(extraction / "skills/arc/scripts/doctor-arc.py"), "--project-dir", str(extraction)], cwd=extraction, env=env, capture_output=True, text=True)
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["providers"]["host"]["available"]
    from ac_llm import LLMRequest, ModelSelection, JsonOutput, request_to_document
    request = extraction / "request.json"
    request.write_text(json.dumps(request_to_document(LLMRequest("bundle-smoke", "Offline fixture: return {answer:42}.", JsonOutput({"type": "object", "properties": {"answer": {"const": 42}}, "required": ["answer"], "additionalProperties": False}), ModelSelection(provider="host")))))
    root = extraction / ".arc/runs"
    common = ["--run-root", str(root), "--run-id", "smoke"]
    def command(*args):
        result = subprocess.run([sys.executable, "-m", "ac_llm.cli", *args], cwd=extraction, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr + result.stdout
        return json.loads(result.stdout)
    assert command("generate", "--request", str(request), *common)["status"] == "paused"
    task = command("host-pending", *common)["data"]["tasks"][0]
    export = command("host-export", "--task-id", task["task_id"], *common)["data"]["task"]
    response = extraction / "response.json"
    output = {"schema_version": "ac.llm.host_turn.v1", "state": "complete", "result": {"answer": 42}, "host_request": None}
    response.write_text(json.dumps({"schema_version": "ac.llm.host_response.v1", "task_id": export["task_id"], "request_sha256": export["request_sha256"], "actor": {"actor_id": "offline", "kind": "fake"}, "output": output}))
    command("host-submit", "--response", str(response), *common)
    assert command("resume", *common)["status"] == "completed"
    command("host-submit", "--response", str(response), *common)
    assert command("resume", *common)["status"] == "completed"
    assert len(command("host-pending", "--all", *common)["data"]["tasks"]) == 1
