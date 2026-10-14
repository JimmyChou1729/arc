"""Explicit portable TeX setup in a new caller-owned directory."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tarfile
import shutil
import uuid
from pathlib import Path

from ac_jobs import InstallationOperation, file_matches_sha256

from .report_environment import ReportEnvironmentError

TEX_LOCK = Path(__file__).with_name("report-tex.json")


def _run(command, env, operation=None):
    if operation is not None:
        return operation.run(command, env=env, timeout=600)["stdout"].strip()
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ReportEnvironmentError(f"TeX setup command failed ({result.returncode}): {result.stderr[-3000:] or result.stdout[-3000:]}")
    return result.stdout.strip()


def _environment(root):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("TEXMF") and key not in {"TEXINPUTS", "TEXFORMATS", "BIBINPUTS", "BSTINPUTS"}}
    env.update(TEXMFHOME=str(root / "texmf-local"), TEXMFVAR=str(root / "texmf-var"),
               TEXMFCONFIG=str(root / "texmf-config"), XDG_CACHE_HOME=str(root / "cache"))
    env.update({name: str(root / "tmp") for name in ("TMPDIR", "TEMP", "TMP")})
    env["PATH"] = str(root / "bin/x86_64-linux") + os.pathsep + env.get("PATH", "")
    return env


def _check(root, operation=None):
    from .report_delivery import REQUIRED_TEX_PACKAGES
    binary = root / "bin/x86_64-linux"
    for name in ("xelatex", "kpsewhich", "tlmgr"):
        path = binary / name
        if not path.resolve().is_relative_to(root.resolve()) or not os.access(path, os.X_OK):
            raise ReportEnvironmentError(f"Owned TeX tool is missing or outside its directory: {name}")
    env = _environment(root)
    for package in REQUIRED_TEX_PACKAGES:
        path = _run([str(binary / "kpsewhich"), package], env, operation)
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


def _operation(destination):
    destination = Path(destination).expanduser().absolute()
    if destination.is_symlink():
        raise ReportEnvironmentError("Owned TeX destination must be a real directory")
    destination = destination.resolve()
    lock = json.loads(TEX_LOCK.read_text())
    lock_sha = hashlib.sha256(TEX_LOCK.read_bytes()).hexdigest()
    operation = InstallationOperation(destination.parent / ("." + destination.name + ".ac-install"),
        {"purpose": "report-tex", "destination": str(destination), "source_lock_sha256": lock_sha})
    return destination, lock, lock_sha, operation


def report_tex_status(destination):
    """Read only the durable installation records; never download/run tools."""
    destination, lock, lock_sha, operation = _operation(destination)
    result = operation.status()
    receipt = destination / "arc-tex-install.json"
    result["destination"] = str(destination)
    result["tex_receipt"] = str(receipt) if receipt.is_file() else None
    result["payload_verification"] = "not_rechecked"
    return result


def setup_report_tex(destination, *, retry=False, archive=None):
    """Explicitly install or recover; mutable failed trees are never replayed."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"} or platform.libc_ver()[0] != "glibc":
        raise ReportEnvironmentError("Owned TeX setup supports Linux x86_64/glibc; other hosts must supply documented report tools.")
    destination, lock, lock_sha, operation = _operation(destination)
    receipt = destination / "arc-tex-install.json"
    status = operation.status()
    if status["lease"]["state"] == "occupied":
        from ac_jobs import RunBusyError
        raise RunBusyError("TeX installation is still active; query --status before retrying")
    if status["state"] == "unverifiable":
        raise ReportEnvironmentError("TeX installation ownership/source cannot be verified: " + status.get("message", str(status["lease"])))
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        if saved.get("source_lock_sha256") != lock_sha:
            raise ReportEnvironmentError("Owned TeX source lock changed; choose a new directory")
        try:
            binary = _check(destination)
        except (ReportEnvironmentError, OSError, subprocess.TimeoutExpired) as exc:
            raise ReportEnvironmentError(f"Published TeX payload is invalid ({exc}); existing files were preserved. Choose a new empty --tex-dir.") from exc
        result = {"bin": binary, "reused": True, "receipt": str(receipt),
                  "operation_id": operation.operation_id, "operation_directory": str(operation.directory)}
        if status["state"] != "succeeded":
            # Reconcile a crash after validated publication but before job completion.
            with operation.begin(retry=True):
                operation.checkpoint("reconciling_published_result")
                operation.complete(result)
        return result
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ReportEnvironmentError("Owned TeX setup requires an empty destination; existing files were preserved. Unknown legacy trees require a new empty --tex-dir.")
    if not hasattr(tarfile, "data_filter"):
        raise ReportEnvironmentError("Portable TeX extraction requires Python 3.11.8+ or 3.12+")
    with operation.begin(retry=retry):
        assert operation.attempt is not None
        work = operation.attempt
        cached = operation.directory / "downloads" / (lock["sha256"] + ".tar.gz")
        cached.parent.mkdir(parents=True, exist_ok=True)
        reused = file_matches_sha256(cached, lock["sha256"], lock["bytes"])
        operation.checkpoint("checking_archive", archive=str(cached), archive_reused=reused)
        if not reused:
            if cached.exists():
                cached.rename(cached.with_name("invalid-" + uuid.uuid4().hex + ".tar.gz"))
            incoming = work / "archive.tar.gz"
            if archive is not None:
                source = Path(archive).expanduser().resolve()
                if not file_matches_sha256(source, lock["sha256"], lock["bytes"]):
                    raise ReportEnvironmentError("Supplied TeX archive failed integrity verification")
                operation.checkpoint("copying_verified_archive", source=str(source))
                shutil.copyfile(source, incoming)
            else:
                operation.checkpoint("downloading_archive", partial_archive=str(incoming))
                _download(lock, incoming)
            if not file_matches_sha256(incoming, lock["sha256"], lock["bytes"]):
                raise ReportEnvironmentError("Portable TeX archive failed integrity verification")
            incoming.rename(cached)
        operation.checkpoint("archive_verified", archive=str(cached), sha256=lock["sha256"])
        operation.checkpoint("extracting")
        with tarfile.open(cached) as source:
            source.extractall(work, filter="data")
        staged = work / ".TinyTeX"
        binary = staged / "bin/x86_64-linux"
        (staged / "tmp").mkdir(exist_ok=True)
        env = _environment(staged)
        manager = str(binary / "tlmgr")
        operation.checkpoint("updating_package_manager")
        _run([manager, "--repository", lock["repository"], "update", "--self"], env, operation)
        operation.checkpoint("installing_packages")
        _run([manager, "--repository", lock["repository"], "install", *lock["packages"]], env, operation)
        operation.checkpoint("validating_staged_tree")
        _check(staged, operation)
        saved = {"schema_version": "arc.report_tex_install.v1", "source_lock_sha256": lock_sha,
                 "source": lock, "operation_id": operation.operation_id, "attempt_id": work.name,
                 "packages": _run([manager, "info", "--only-installed", "--data", "name,localrev"], env, operation),
                 "xelatex": _run([str(binary / "xelatex"), "--version"], env, operation)}
        from ac_jobs import atomic_write_json
        atomic_write_json(staged / receipt.name, saved)
        operation.checkpoint("publishing")
        if destination.exists():
            destination.rmdir()
        staged.rename(destination)
        operation.checkpoint("validating_published_tree")
        result = {"bin": _check(destination, operation), "reused": False, "receipt": str(receipt),
                  "operation_id": operation.operation_id, "operation_directory": str(operation.directory), "archive_reused": reused}
        operation.complete(result)
    return result
