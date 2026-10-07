"""Explicit report-font provisioning and read-only profile resolution."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path


FONT_LOCK = Path(__file__).with_name("report-fonts.json")


class ReportEnvironmentError(ValueError):
    pass


def font_lock():
    return json.loads(FONT_LOCK.read_text(encoding="utf-8"))


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_report_environment(path=None):
    """Resolve a caller-selected profile without downloading or changing it."""
    selected = path if path is not None else os.environ.get("ARC_REPORT_ENVIRONMENT")
    if not selected:
        return None
    try:
        config_path = Path(selected).expanduser().resolve()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        lock = font_lock()
        if config.get("schema_version") != "arc.report_environment.v1" or config.get("font_lock_sha256") != _digest(FONT_LOCK):
            raise ReportEnvironmentError("Report profile does not match the current font lock; run explicit setup into a new directory.")
        tools = config.get("tools")
        if not isinstance(tools, dict) or set(tools) != {"pandoc", "xelatex", "kpsewhich"}:
            raise ReportEnvironmentError("Report tools must contain exactly pandoc, xelatex and kpsewhich paths.")
        for entry in lock["files"]:
            target = config_path.parent / entry["name"]
            if not target.is_file() or target.stat().st_size != entry["bytes"] or _digest(target) != entry["sha256"]:
                raise ReportEnvironmentError(f"Report font/license failed integrity verification: {entry['name']}")
        for name in ("pandoc", "xelatex", "kpsewhich"):
            value = config.get("tools", {}).get(name)
            if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file() or not os.access(value, os.X_OK):
                raise ReportEnvironmentError(f"Configured report tool is missing: {name}")
        return {**config, "path": str(config_path), "font_directory": str(config_path.parent)}
    except (OSError, json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise ReportEnvironmentError(f"Report profile is unreadable or malformed: {type(exc).__name__}") from exc


def report_process_environment(profile):
    env = dict(os.environ)
    if profile:
        directories = list(dict.fromkeys(str(Path(path).parent) for path in profile["tools"].values()))
        env["PATH"] = os.pathsep.join([*directories, env.get("PATH", "")])
    return env


def setup_report_environment(destination, *, fetch=None):
    """Install verified fonts only when explicitly invoked; retain the OFL."""
    destination = Path(destination).expanduser().absolute()
    if destination.is_symlink():
        raise ReportEnvironmentError("Report setup destination must be a real directory.")
    existing = destination / "report-environment.json"
    if existing.is_file():
        load_report_environment(existing)
        return {"status": "ready", "reused": True, "environment": str(existing)}
    if destination.exists() and any(destination.iterdir()):
        raise ReportEnvironmentError("Report setup requires an empty destination; existing files were preserved.")
    tools = {name: shutil.which(name) for name in ("pandoc", "xelatex", "kpsewhich")}
    missing = [name for name, path in tools.items() if path is None]
    if missing:
        raise ReportEnvironmentError("Missing report tools: " + ", ".join(missing) + ". Install the documented system report prerequisites first.")
    if fetch is None:
        import httpx
        def fetch(url):
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.content
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".arc-report-setup-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "profile"
        staged.mkdir()
        for entry in font_lock()["files"]:
            payload = fetch(entry["url"])
            if len(payload) != entry["bytes"] or hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                raise ReportEnvironmentError(f"Downloaded resource failed verification: {entry['name']}")
            (staged / entry["name"]).write_bytes(payload)
        config = {"schema_version": "arc.report_environment.v1", "font_lock_sha256": _digest(FONT_LOCK),
                  "tools": tools, "font_source": font_lock()["upstream_commit"]}
        (staged / "report-environment.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        load_report_environment(staged / "report-environment.json")
        if destination.exists():
            destination.rmdir()
        staged.rename(destination)
    return {"status": "ready", "reused": False, "environment": str(existing)}
