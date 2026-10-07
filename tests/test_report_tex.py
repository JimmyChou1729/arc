from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/arc/skills/arc/scripts"
sys.path.insert(0, str(SCRIPTS))
tex = importlib.import_module("_arc_workflows.report_tex")
sys.path.remove(str(SCRIPTS))


@pytest.fixture
def owned_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(tex.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tex.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tex.platform, "libc_ver", lambda: ("glibc", "2.39"))
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name in ("xelatex", "kpsewhich", "tlmgr"):
            entry = tarfile.TarInfo(".TinyTeX/bin/x86_64-linux/" + name)
            entry.mode, entry.size = 0o755, 4
            archive.addfile(entry, io.BytesIO(b"fake"))
    archive_bytes = payload.getvalue()
    lock = tmp_path / "source.json"
    lock.write_text(json.dumps({"url": "https://fixture.invalid/archive", "bytes": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(), "repository": "https://fixture.invalid/frozen", "packages": ["xecjk"]}))
    monkeypatch.setattr(tex, "TEX_LOCK", lock)
    @contextlib.contextmanager
    def stream(*args, **kwargs):
        yield httpx.Response(200, content=archive_bytes, request=httpx.Request("GET", "https://fixture.invalid/archive"))
    monkeypatch.setattr(httpx, "stream", stream)
    commands = []
    def run(command, env):
        assert Path(command[0]).is_relative_to(tmp_path)
        assert env["PATH"].split(":")[0] == str(Path(command[0]).parent)
        commands.append(command)
        if command[1] in {"--version", "info"}:
            return "fixture provenance"
        if command[0].endswith("kpsewhich"):
            return str(Path(command[0]).parents[2] / command[1])
        return ""
    monkeypatch.setattr(tex, "_run", run)
    return tmp_path / "owned", commands


def test_owned_setup_installs_only_inside_destination_and_reuses(owned_setup):
    destination, commands = owned_setup
    result = tex.setup_report_tex(destination)
    assert result["reused"] is False
    assert json.loads(Path(result["receipt"]).read_text())["packages"] == "fixture provenance"
    installs = [c for c in commands if "install" in c]
    assert len(installs) == 1 and installs[0][-1] == "xecjk"
    commands.clear()
    assert tex.setup_report_tex(destination)["reused"] is True
    assert all("install" not in c for c in commands)


def test_failed_download_never_publishes(owned_setup):
    destination, commands = owned_setup
    lock = json.loads(tex.TEX_LOCK.read_text())
    lock["sha256"] = "0" * 64
    tex.TEX_LOCK.write_text(json.dumps(lock))
    with pytest.raises(tex.ReportEnvironmentError, match="integrity"):
        tex.setup_report_tex(destination)
    assert not destination.exists() and not commands


def test_install_failure_preserves_empty_destination_for_retry(owned_setup, monkeypatch):
    destination, _ = owned_setup
    destination.mkdir()
    def fail(*args):
        raise tex.ReportEnvironmentError("install failed")
    monkeypatch.setattr(tex, "_run", fail)
    with pytest.raises(tex.ReportEnvironmentError, match="install failed"):
        tex.setup_report_tex(destination)
    assert list(destination.iterdir()) == []


def test_existing_unrelated_tree_is_preserved(owned_setup):
    destination, commands = owned_setup
    destination.mkdir()
    (destination / "user.txt").write_text("keep")
    with pytest.raises(tex.ReportEnvironmentError, match="preserved"):
        tex.setup_report_tex(destination)
    assert (destination / "user.txt").read_text() == "keep" and not commands


def test_reuse_rejects_missing_owned_binary(owned_setup):
    destination, _ = owned_setup
    tex.setup_report_tex(destination)
    (destination / "bin/x86_64-linux/xelatex").unlink()
    with pytest.raises(tex.ReportEnvironmentError, match="missing"):
        tex.setup_report_tex(destination)


def test_platform_is_explicitly_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tex.platform, "system", lambda: "Darwin")
    with pytest.raises(tex.ReportEnvironmentError, match="Linux"):
        tex.setup_report_tex(tmp_path / "tex")
