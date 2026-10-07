"""Explicit portable TeX setup in a new caller-owned directory."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .report_environment import ReportEnvironmentError

TEX_LOCK = Path(__file__).with_name("report-tex.json")


def _run(command, env):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ReportEnvironmentError(f"TeX setup command failed ({result.returncode}): {result.stderr[-3000:] or result.stdout[-3000:]}")
    return result.stdout.strip()


def _environment(root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("TEXMF") and key not in {"TEXINPUTS", "TEXFORMATS", "BIBINPUTS", "BSTINPUTS"}}
    env.update(TEXMFHOME=str(root / "texmf-local"), TEXMFVAR=str(root / "texmf-var"),
               TEXMFCONFIG=str(root / "texmf-config"), XDG_CACHE_HOME=str(root / "cache"))
    env["PATH"] = str(root / "bin/x86_64-linux") + os.pathsep + env.get("PATH", "")
    return env


def _check(root):
    from .report_delivery import REQUIRED_TEX_PACKAGES
    binary = root / "bin/x86_64-linux"
    for name in ("xelatex", "kpsewhich", "tlmgr"):
        path = binary / name
        if not path.resolve().is_relative_to(root.resolve()) or not os.access(path, os.X_OK):
            raise ReportEnvironmentError(f"Owned TeX tool is missing or outside its directory: {name}")
    env = _environment(root)
    for package in REQUIRED_TEX_PACKAGES:
        path = _run([str(binary / "kpsewhich"), package], env)
        if not path or not Path(path).resolve().is_relative_to(root.resolve()):
            raise ReportEnvironmentError(f"Owned TeX package is missing: {package}")
    return str(binary)


def _download(lock, destination):
    import httpx
    digest, size = hashlib.sha256(), 0
    with httpx.stream("GET", lock["url"], follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    if size != lock["bytes"] or digest.hexdigest() != lock["sha256"]:
        raise ReportEnvironmentError("Portable TeX archive failed integrity verification")


def setup_report_tex(destination):
    """Download/install only on explicit invocation; never invoke system tlmgr."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"} or platform.libc_ver()[0] != "glibc":
        raise ReportEnvironmentError("Owned TeX setup supports Linux x86_64/glibc; other hosts must supply documented report tools.")
    destination = Path(destination).expanduser().absolute()
    if destination.is_symlink():
        raise ReportEnvironmentError("Owned TeX destination must be a real directory")
    lock = json.loads(TEX_LOCK.read_text())
    lock_sha = hashlib.sha256(TEX_LOCK.read_bytes()).hexdigest()
    receipt = destination / "arc-tex-install.json"
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        if saved.get("source_lock_sha256") != lock_sha:
            raise ReportEnvironmentError("Owned TeX source lock changed; choose a new directory")
        return {"bin": _check(destination), "reused": True, "receipt": str(receipt)}
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ReportEnvironmentError("Owned TeX setup requires an empty destination; existing files were preserved")
    if not hasattr(tarfile, "data_filter"):
        raise ReportEnvironmentError("Portable TeX extraction requires Python 3.11.8+ or 3.12+")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".arc-tex-setup-", dir=destination.parent) as temporary:
        work = Path(temporary)
        archive = work / "tinytex.tar.gz"
        _download(lock, archive)
        with tarfile.open(archive) as source:
            source.extractall(work, filter="data")
        staged = work / ".TinyTeX"
        binary = staged / "bin/x86_64-linux"
        env = _environment(staged)
        manager = str(binary / "tlmgr")
        _run([manager, "--repository", lock["repository"], "update", "--self"], env)
        _run([manager, "--repository", lock["repository"], "install", *lock["packages"]], env)
        _check(staged)
        saved = {"schema_version": "arc.report_tex_install.v1", "source_lock_sha256": lock_sha,
                 "source": lock, "packages": _run([manager, "info", "--only-installed", "--data", "name,revision"], env),
                 "xelatex": _run([str(binary / "xelatex"), "--version"], env)}
        (staged / receipt.name).write_text(json.dumps(saved, indent=2) + "\n")
        if destination.exists():
            destination.rmdir()
        staged.rename(destination)
    return {"bin": _check(destination), "reused": False, "receipt": str(receipt)}
