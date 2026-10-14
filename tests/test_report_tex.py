from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import signal
import shutil
import subprocess
import sys
import tarfile
import time
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
    def run(command, env, operation=None):
        assert Path(command[0]).is_relative_to(tmp_path)
        assert env["PATH"].split(":")[0] == str(Path(command[0]).parent)
        commands.append(command)
        if command[1] == "info":
            assert command[-2:] == ["--data", "name,localrev"]
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
    with pytest.raises(tex.ReportEnvironmentError, match="new empty --tex-dir"):
        tex.setup_report_tex(destination)


def test_platform_is_explicitly_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(tex.platform, "system", lambda: "Darwin")
    with pytest.raises(tex.ReportEnvironmentError, match="Linux"):
        tex.setup_report_tex(tmp_path / "tex")


def test_status_is_readonly_before_install(owned_setup):
    destination, commands = owned_setup
    before = sorted(destination.parent.iterdir())
    assert tex.report_tex_status(destination)["state"] == "not_started"
    assert sorted(destination.parent.iterdir()) == before and not commands


def test_retry_preserves_failed_tree_and_reuses_verified_archive(owned_setup, monkeypatch):
    from ac_jobs import InstallationRetryRequiredError
    destination, commands = owned_setup
    original = tex._run
    def fail(*args):
        raise tex.ReportEnvironmentError("fixture install interrupted")
    monkeypatch.setattr(tex, "_run", fail)
    with pytest.raises(tex.ReportEnvironmentError):
        tex.setup_report_tex(destination)
    first = tex.report_tex_status(destination)
    old_tree = Path(first["directory"]) / "attempts" / first["current"]["attempt_id"] / ".TinyTeX"
    (old_tree / "partial.txt").write_text("retained")
    monkeypatch.setattr(tex, "_run", original)
    with pytest.raises(InstallationRetryRequiredError):
        tex.setup_report_tex(destination)
    def forbidden(*args):
        raise AssertionError("retry downloaded an already verified archive")
    monkeypatch.setattr(tex, "_download", forbidden)
    result = tex.setup_report_tex(destination, retry=True)
    assert result["archive_reused"] and not (destination / "partial.txt").exists()
    assert (old_tree / "partial.txt").read_text() == "retained"
    status = tex.report_tex_status(destination)
    assert status["state"] == "succeeded"
    assert status["current"]["previous_attempt"] == first["current"]["attempt_id"]


def test_crash_after_publication_reconciles_without_install(owned_setup, monkeypatch):
    from ac_jobs import InstallationOperation
    destination, commands = owned_setup
    complete = InstallationOperation.complete
    def fail(*args):
        raise RuntimeError("crash after rename")
    monkeypatch.setattr(InstallationOperation, "complete", fail)
    with pytest.raises(RuntimeError, match="after rename"):
        tex.setup_report_tex(destination)
    assert (destination / "arc-tex-install.json").is_file()
    assert tex.report_tex_status(destination)["state"] == "failed"
    monkeypatch.setattr(InstallationOperation, "complete", complete)
    commands.clear()
    assert tex.setup_report_tex(destination, retry=True)["reused"]
    assert not any("install" in c for c in commands)
    assert tex.report_tex_status(destination)["state"] == "succeeded"


def test_failed_published_payload_is_preserved_and_never_completed(owned_setup, monkeypatch):
    destination, _ = owned_setup
    check = tex._check
    def fail_published(root, operation=None):
        if root == destination:
            raise tex.ReportEnvironmentError("fixture invalid payload")
        return check(root, operation)
    monkeypatch.setattr(tex, "_check", fail_published)
    with pytest.raises(tex.ReportEnvironmentError):
        tex.setup_report_tex(destination)
    with pytest.raises(tex.ReportEnvironmentError, match="new empty --tex-dir"):
        tex.setup_report_tex(destination, retry=True)
    assert tex.report_tex_status(destination)["state"] == "failed"
    assert (destination / "arc-tex-install.json").is_file()


def test_status_cli_without_initialized_foundation_returns_json(tmp_path):
    scripts = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, scripts)
    env = {k: v for k, v in os.environ.items() if k not in {"AC_FOUNDATION_REPO_ROOT", "ARC_REQUIRE_REPO_ROOT", "PYTHONPATH"}}
    result = subprocess.run([sys.executable, "-S", str(scripts / "setup-report.py"), "--status", "--tex-dir", str(tmp_path / "tex")], env=env, capture_output=True, text=True)
    assert result.returncode == 1 and not result.stderr
    payload = json.loads(result.stdout)
    assert payload["code"] == "report_runtime_unavailable" and "arc-runtime setup" in payload["guidance"]
    assert not (tmp_path / ".tex.ac-install").exists()


def test_status_cli_needs_no_pandoc_and_creates_nothing(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPTS / "setup-report.py"), "--status", "--tex-dir", str(tmp_path / "tex")],
                            env={**os.environ, "PATH": ""}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["tex"]["state"] == "not_started"
    assert not (tmp_path / ".tex.ac-install").exists()


@pytest.mark.skipif(os.name != "posix", reason="inherited POSIX installation lease")
def test_real_tex_command_survives_coordinator_kill_and_retry_isolated(tmp_path, monkeypatch):
    from ac_jobs import RunBusyError
    payload = io.BytesIO()
    body = f'''#!{sys.executable}
import pathlib,sys,time
root=pathlib.Path(__file__).resolve().parents[2]
fixture=pathlib.Path({str(tmp_path)!r})
if pathlib.Path(sys.argv[0]).name=='kpsewhich': print(root/sys.argv[1])
elif 'install' in sys.argv:
 (root/'partial.txt').write_text('original attempt')
 if not (fixture/'release').exists():
  (fixture/'started').touch()
  print('installation still running',flush=True)
  while not (fixture/'release').exists():time.sleep(.02)
else: print('fixture provenance')
'''.encode()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name in ("xelatex", "kpsewhich", "tlmgr"):
            entry = tarfile.TarInfo(".TinyTeX/bin/x86_64-linux/" + name)
            entry.mode, entry.size = 0o755, len(body)
            archive.addfile(entry, io.BytesIO(body))
    archive = tmp_path / "verified.tar.gz"; archive.write_bytes(payload.getvalue())
    lock = tmp_path / "source.json"
    lock.write_text(json.dumps({"url": "https://fixture.invalid", "bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "repository": "https://fixture.invalid", "packages": ["xecjk"]}))
    monkeypatch.setattr(tex, "TEX_LOCK", lock)
    monkeypatch.setattr(tex.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tex.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tex.platform, "libc_ver", lambda: ("glibc", "2.39"))
    destination = tmp_path / "owned"
    script = tmp_path / "owner.py"
    script.write_text(f'''import sys
from pathlib import Path
sys.path.insert(0,{str(SCRIPTS)!r})
from _arc_workflows import report_tex as tex
tex.TEX_LOCK=Path({str(lock)!r})
tex.platform.system=lambda:'Linux'
tex.platform.machine=lambda:'x86_64'
tex.platform.libc_ver=lambda:('glibc','2.39')
tex.setup_report_tex({str(destination)!r},archive={str(archive)!r})
''')
    def wait(predicate):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(.02)
        raise AssertionError("TeX subprocess fixture did not reach state")
    process = subprocess.Popen([sys.executable, str(script)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait(lambda: (tmp_path / "started").exists())
        process.kill(); process.wait(timeout=5)
        status = tex.report_tex_status(destination)
        assert status["state"] == "running" and status["current"]["phase"] == "installing_packages"
        with pytest.raises(RunBusyError): tex.setup_report_tex(destination, retry=True)
        (tmp_path / "release").touch()
        wait(lambda: tex.report_tex_status(destination)["lease"]["state"] == "available")
        assert tex.report_tex_status(destination)["state"] == "interrupted"
        result = tex.setup_report_tex(destination, retry=True)
        assert result["archive_reused"] and tex.report_tex_status(destination)["state"] == "succeeded"
        attempts = list((Path(status["directory"]) / "attempts").iterdir())
        assert len(attempts) == 2
        assert (attempts[[p.name for p in attempts].index(status["current"]["attempt_id"])] / ".TinyTeX/partial.txt").is_file()
    finally:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=5)
