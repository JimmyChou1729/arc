"""Private-runtime bootstrap shared by AC Foundation product launchers."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

LAUNCHER_VERSION = 2
LOCK_SCHEMA = "ac.runtime_sources.v2"
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9-]*")


class RuntimeConfigError(ValueError):
    """Raised when the checked-in source lock is unusable."""


@dataclass(frozen=True)
class Source:
    source_id: str
    repository: str
    commit: str
    packages: tuple[str, ...]
    tools: tuple[str, ...]
    local_root_env: str


@dataclass(frozen=True)
class RuntimeLock:
    profile: str
    sources: tuple[Source, ...]
    environment_defaults: dict[str, str]

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(tool for source in self.sources for tool in source.tools)


def _die(message: str, code: int = 78) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{label} must be an object")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise RuntimeConfigError(f"{label} must be a non-empty array")
    result = tuple(_string(item, label) for item in value)
    if len(set(result)) != len(result):
        raise RuntimeConfigError(f"{label} must not contain duplicates")
    return result


def load_lock(path: Path) -> RuntimeLock:
    try:
        document = _object(json.loads(path.read_text(encoding="utf-8")), "source lock")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(f"cannot read source lock {path}: {exc}") from exc
    if document.get("schema_version") != LOCK_SCHEMA:
        raise RuntimeConfigError(f"source lock schema must be {LOCK_SCHEMA}")
    if set(document) != {
        "schema_version",
        "profile",
        "sources",
        "environment_defaults",
    }:
        raise RuntimeConfigError("source lock contains missing or unknown fields")
    profile = _string(document["profile"], "profile")
    if not IDENTIFIER_RE.fullmatch(profile):
        raise RuntimeConfigError("profile must be a lowercase identifier")
    raw_sources = document["sources"]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise RuntimeConfigError("sources must be a non-empty array")
    sources: list[Source] = []
    seen_packages: set[str] = set()
    seen_tools: set[str] = set()
    for index, raw in enumerate(raw_sources):
        item = _object(raw, f"sources[{index}]")
        expected = {
            "id",
            "repository",
            "commit",
            "packages",
            "tools",
            "local_root_env",
        }
        if set(item) != expected:
            raise RuntimeConfigError(f"sources[{index}] contains missing or unknown fields")
        source_id = _string(item["id"], f"sources[{index}].id")
        repository = _string(item["repository"], f"sources[{index}].repository")
        commit = _string(item["commit"], f"sources[{index}].commit").lower()
        packages = _string_list(item["packages"], f"sources[{index}].packages")
        tools = _string_list(item["tools"], f"sources[{index}].tools")
        local_root_env = _string(
            item["local_root_env"], f"sources[{index}].local_root_env"
        )
        if not IDENTIFIER_RE.fullmatch(source_id):
            raise RuntimeConfigError(f"invalid source id: {source_id}")
        parsed_repository = urlsplit(repository)
        if (
            parsed_repository.scheme != "https"
            or not parsed_repository.hostname
            or parsed_repository.username is not None
            or parsed_repository.password is not None
            or parsed_repository.query
            or parsed_repository.fragment
            or not parsed_repository.path.endswith(".git")
        ):
            raise RuntimeConfigError(f"source repository must be an HTTPS Git URL: {repository}")
        if not COMMIT_RE.fullmatch(commit):
            raise RuntimeConfigError(f"source commit must be a full Git SHA: {commit}")
        if not local_root_env.startswith("AC_"):
            raise RuntimeConfigError("local_root_env must be AC-owned")
        if seen_packages.intersection(packages) or seen_tools.intersection(tools):
            raise RuntimeConfigError("packages and tools must have one owning source")
        seen_packages.update(packages)
        seen_tools.update(tools)
        sources.append(
            Source(
                source_id,
                repository,
                commit,
                packages,
                tools,
                local_root_env,
            )
        )
    raw_defaults = _object(document["environment_defaults"], "environment_defaults")
    defaults: dict[str, str] = {}
    for key, value in raw_defaults.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RuntimeConfigError(f"invalid environment key: {key!r}")
        defaults[key] = _string(value, f"environment_defaults.{key}")
    return RuntimeLock(profile, tuple(sources), defaults)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_hash(root: Path, packages: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for package in packages:
        package_root = root / "packages" / package
        if not (package_root / "pyproject.toml").is_file():
            raise RuntimeConfigError(f"local source lacks package {package}: {root}")
        for path in sorted(package_root.rglob("*")):
            if not path.is_file() or any(
                part in {".venv", "__pycache__", "build", "dist"}
                or part.endswith(".egg-info")
                for part in path.parts
            ):
                continue
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _local_roots(lock: RuntimeLock) -> dict[str, Path] | None:
    roots: dict[str, Path] = {}
    for source in lock.sources:
        value = os.environ.get(source.local_root_env)
        if not value:
            return None
        root = Path(value).expanduser().resolve()
        for package in source.packages:
            if not (root / "packages" / package / "pyproject.toml").is_file():
                return None
        roots[source.source_id] = root
    return roots


def _expanded_runtime_path(value: str | Path) -> Path:
    """Make a runtime path absolute without resolving a private-venv symlink."""

    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def _venv_bin_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "venv" / ("Scripts" if os.name == "nt" else "bin")


def _venv_python(runtime_dir: Path) -> Path:
    return _venv_bin_dir(runtime_dir) / ("python.exe" if os.name == "nt" else "python")


def _venv_tool(runtime_dir: Path, tool: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _venv_bin_dir(runtime_dir) / f"{tool}{suffix}"


def _private_runtime_environment(
    runtime_dir: Path, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Return child environment with this literal private venv path first.

    In particular, do not call ``resolve()`` here.  Console-script shebangs can
    point through a symlinked Python, while sibling private commands still need
    the original venv ``bin``/``Scripts`` directory on ``PATH``.
    """

    result = dict(os.environ if environ is None else environ)
    private_bin = str(_venv_bin_dir(runtime_dir))
    inherited = result.get("PATH")
    result["PATH"] = (
        private_bin if not inherited else f"{private_bin}{os.pathsep}{inherited}"
    )
    return result


def _source_selection(lock: RuntimeLock) -> tuple[str, dict[str, Path] | None]:
    requested = os.environ.get("AC_INSTALL_SOURCE", "auto")
    if requested not in {"auto", "local", "git", "mixed"}:
        raise RuntimeConfigError("AC_INSTALL_SOURCE must be auto, local, git, or mixed")
    if requested == "mixed":
        roots: dict[str, Path] = {}
        for source in lock.sources:
            value = os.environ.get(source.local_root_env)
            if value is None:
                continue
            if not value.strip():
                raise RuntimeConfigError(f"mixed install root {source.local_root_env} is empty")
            root = Path(value).expanduser().resolve()
            for package in source.packages:
                if not (root / "packages" / package / "pyproject.toml").is_file():
                    raise RuntimeConfigError(
                        f"mixed install root {source.local_root_env} lacks packages/{package}/pyproject.toml"
                    )
            roots[source.source_id] = root
        return "mixed", roots
    roots = _local_roots(lock)
    if requested == "local" and roots is None:
        missing = ", ".join(source.local_root_env for source in lock.sources)
        raise RuntimeConfigError(f"local install requires complete roots: {missing}")
    if requested == "local" or (requested == "auto" and roots is not None):
        return "local", roots
    return "git", None


def _expand_default(value: str, *, cwd: Path, ac_home: Path) -> str:
    return value.replace("{cwd}", str(cwd)).replace("{ac_home}", str(ac_home))


def _runtime_environment(lock: RuntimeLock, roots: dict[str, Path] | None) -> dict[str, str]:
    cwd = Path.cwd().resolve()
    ac_home = _expanded_runtime_path(os.environ.get("AC_HOME", Path.home() / ".ac"))
    os.environ["AC_HOME"] = str(ac_home)
    runtime_home = _expanded_runtime_path(
        os.environ.get("AC_RUNTIME_HOME", ac_home / "runtimes")
    )
    os.environ["AC_RUNTIME_HOME"] = str(runtime_home)
    if "AC_DOCUMENT_CACHE" not in os.environ:
        cache = cwd / ".ac" / "cache" / "ac-document"
        if roots is not None:
            for root in roots.values():
                if cwd == root or root in cwd.parents:
                    cache = root / "local" / "cache" / "ac-document"
                    break
        os.environ["AC_DOCUMENT_CACHE"] = str(cache)
    environment = {
        "AC_HOME": str(ac_home),
        "AC_RUNTIME_HOME": str(runtime_home),
        "AC_DOCUMENT_CACHE": os.environ["AC_DOCUMENT_CACHE"],
    }
    for key, value in lock.environment_defaults.items():
        os.environ.setdefault(key, _expand_default(value, cwd=cwd, ac_home=ac_home))
        environment[key] = os.environ[key]
    return environment


def _fingerprint(
    lock_path: Path,
    lock: RuntimeLock,
    mode: str,
    roots: dict[str, Path] | None,
    constraints_path: Path,
    extra_requirements: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    identity: dict[str, Any] = {
        "launcher_version": LAUNCHER_VERSION,
        "lock_sha256": _sha256(lock_path.read_bytes()),
        "mode": mode,
        "python": [str(Path(sys.executable).resolve()), list(sys.version_info[:3])],
        "constraints_sha256": (
            _sha256(constraints_path.read_bytes()) if constraints_path.is_file() else None
        ),
        "sources": [],
    }
    if extra_requirements:
        identity["extra_requirements"] = list(extra_requirements)
    for source in lock.sources:
        source_identity: dict[str, Any] = {
            "id": source.source_id,
            "repository": source.repository,
            "commit": source.commit,
            "packages": source.packages,
        }
        if mode == "mixed":
            source_identity["mode"] = "local" if roots and source.source_id in roots else "git"
        if roots is not None and source.source_id in roots:
            root = roots[source.source_id]
            source_identity.update(
                root=str(root),
                revision=_git_revision(root),
                content_sha256=_content_hash(root, source.packages),
            )
        identity["sources"].append(source_identity)
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return _sha256(encoded), identity


def _git_revision(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "no-vcs-revision"


def _atomic_json(path: Path, document: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _diagnostic(code: str, **details: Any) -> None:
    print(json.dumps({"schema_version": "ac.runtime_event.v1", "code": code, **details}), file=sys.stderr, flush=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class InstallLock:
    """Permanent inode, kernel ownership; owner records are diagnostic only."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.record = path.with_name("install.owner.json")

    def __enter__(self) -> InstallLock:
        try:
            import fcntl
        except ImportError:
            _diagnostic("lock_ownership_unverifiable", reason="POSIX flock unavailable", path=str(self.path))
            _die("runtime installation requires a filesystem with POSIX flock support", 75)
        try:
            timeout = float(os.environ.get("AC_INSTALL_LOCK_TIMEOUT_SEC", "600"))
        except ValueError:
            _die("AC_INSTALL_LOCK_TIMEOUT_SEC must be a finite non-negative number")
        if timeout < 0 or not math.isfinite(timeout):
            _die("AC_INSTALL_LOCK_TIMEOUT_SEC must be a finite non-negative number")
        deadline = time.monotonic() + timeout
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            waiting = False
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waiting:
                        _diagnostic("lock_occupied", path=str(self.path), owner_record=_read_json(self.record))
                        waiting = True
                    if time.monotonic() >= deadline:
                        _diagnostic("lock_wait_timeout", path=str(self.path), timeout_seconds=timeout)
                        _die(f"timed out waiting for runtime install lock: {self.path}; an installer may still be running; no takeover was attempted", 75)
                    time.sleep(0.1)
            previous = _read_json(self.record)
            if previous.get("state") == "held":
                _diagnostic("lock_recovered", path=str(self.path), previous_owner=previous,
                            evidence="kernel lock acquired; previous interruption cause unknown")
            elif self.record.exists() and not previous:
                _diagnostic("lock_record_unreadable", path=str(self.record), evidence="kernel lock acquired")
            _atomic_json(self.record, {"state": "held", "pid": os.getpid(), "host": socket.gethostname(),
                                      "acquired_at": time.time(), "previous": previous.get("acquired_at")})
            return self
        except OSError as exc:
            _diagnostic("lock_ownership_unverifiable", path=str(self.path), errno=exc.errno, error=str(exc))
            self._close()
            _die("cannot establish exclusive runtime ownership; use a supported private runtime filesystem", 75)
        except BaseException:
            self._close()
            raise

    def _close(self) -> None:
        if self.fd is not None:
            # LOCK_UN would release descriptors inherited by a surviving installer.
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *_: object) -> None:
        try:
            owner = _read_json(self.record)
            _atomic_json(self.record, {**owner, "state": "released", "released_at": time.time()})
        finally:
            self._close()


def _lock_diagnostic(path: Path) -> dict[str, Any]:
    """Probe an existing inode without creating files or changing owner records."""
    try:
        import fcntl
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return {"state": "not_created"}
    except (ImportError, OSError) as exc:
        return {"state": "ownership_unverifiable", "reason": type(exc).__name__}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "occupied", "evidence": "kernel lock"}
        except OSError as exc:
            return {"state": "ownership_unverifiable", "errno": exc.errno}
        return {"state": "available", "evidence": "kernel lock; process identity not inferred"}
    finally:
        os.close(fd)


def _installation_environment(runtime_dir: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    env = dict(os.environ)
    env["UV_PYTHON_DOWNLOADS"] = "never"
    paths: dict[str, dict[str, Any]] = {"runtime": {"path": str(runtime_dir), "source": "AC_RUNTIME_HOME", "active": True}}
    for name, relative, disabled in (("UV_CACHE_DIR", "cache/uv", "UV_NO_CACHE"), ("PIP_CACHE_DIR", "cache/pip", "PIP_NO_CACHE_DIR")):
        explicit = name in env
        if explicit and not env[name].strip():
            raise RuntimeConfigError(f"{name} must not be empty")
        env[name] = str(_expanded_runtime_path(env[name] if explicit else runtime_dir / relative))
        paths[name] = {"path": env[name], "source": "explicit" if explicit else "private_default",
                       "active": env.get(disabled, "").lower() not in {"1", "true", "yes", "on"}}
    temp_key = next((name for name in ("TMPDIR", "TEMP", "TMP") if env.get(name)), None)
    temporary = _expanded_runtime_path(env[temp_key] if temp_key else runtime_dir / "tmp")
    # All installer descendants see the same selected temp directory.
    env.update({name: str(temporary) for name in ("TMPDIR", "TEMP", "TMP")})
    paths["temporary"] = {"path": str(temporary), "source": temp_key or "private_default", "active": True}
    return env, paths


def _path_diagnostic(path: Path) -> dict[str, Any]:
    """Read-only estimate; setup verifies with a real write before installation."""
    ancestor = path
    try:
        while not ancestor.exists():
            if ancestor == ancestor.parent:
                break
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            return {"writable": False, "reason": "not_a_directory", "checked_ancestor": str(ancestor)}
        read_only = bool(os.statvfs(ancestor).f_flag & getattr(os, "ST_RDONLY", 1)) if hasattr(os, "statvfs") else False
        return {"writable": not read_only and os.access(ancestor, os.W_OK | os.X_OK),
                "reason": "read_only_filesystem" if read_only else "access_estimate", "checked_ancestor": str(ancestor)}
    except OSError as exc:
        return {"writable": None, "reason": "unverifiable", "errno": exc.errno}


def _prepare_install_paths(paths: dict[str, dict[str, Any]]) -> None:
    for name, selected in paths.items():
        if not selected["active"]:
            continue
        path = Path(selected["path"])
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b"runtime path probe")
                probe.flush()
                os.fsync(probe.fileno())
        except OSError as exc:
            _diagnostic("install_path_unwritable", selection=name, path=str(path), errno=exc.errno, error=str(exc))
            raise RuntimeConfigError(f"Cannot write selected {name} path {path}: {exc}. Configure that path to a writable directory; HOME is unchanged.") from exc


def _requirements(lock: RuntimeLock, mode: str, roots: dict[str, Path] | None) -> list[str]:
    requirements: list[str] = []
    for source in lock.sources:
        if mode == "local" or (mode == "mixed" and roots is not None and source.source_id in roots):
            assert roots is not None
            requirements.extend(
                str(roots[source.source_id] / "packages" / package)
                for package in source.packages
            )
        else:
            base = f"git+{source.repository}@{source.commit}"
            requirements.extend(
                f"{package} @ {base}#subdirectory=packages/{package}"
                for package in source.packages
            )
    return requirements


def load_extra_requirements(path: Path | None, lock: RuntimeLock) -> tuple[str, ...]:
    """Read an explicit pinned optional environment without overriding sources."""
    if path is None:
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RuntimeConfigError(f"Cannot read optional requirements: {path}") from exc
    normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
    source_packages = {normalize(package) for source in lock.sources for package in source.packages}
    requirements = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][A-Za-z0-9.!+-]*)", line)
        if match is None:
            raise RuntimeConfigError(f"Optional requirement must be a plain exact package pin at line {number}")
        name = normalize(match[1])
        if name in source_packages or name in requirements:
            raise RuntimeConfigError(f"Optional requirement duplicates or overrides a locked source: {name}")
        requirements[name] = f"{name}=={match[2]}"
    if not requirements:
        raise RuntimeConfigError("Optional requirements must not be empty")
    return tuple(requirements[name] for name in sorted(requirements))


def _run_logged(command: list[str], log_path: Path, *, env: dict[str, str] | None = None, lock_fd: int | None = None) -> None:
    with log_path.open("a", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   errors="replace", env=env, pass_fds=() if lock_fd is None else (lock_fd,))
        log.write(f"[ac-runtime] started {Path(command[0]).name} pid={process.pid}\n")
        log.flush()
        assert process.stdout is not None
        try:
            for line in process.stdout:
                log.write(re.sub(r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", r"\1[REDACTED]@", line))
                log.flush()
            status = process.wait()
        finally:
            process.stdout.close()
        if status != 0:
            raise RuntimeError(f"command failed with exit status {status}")


def _install(
    runtime_dir: Path,
    lock: RuntimeLock,
    mode: str,
    roots: dict[str, Path] | None,
    constraints_path: Path,
    fingerprint: str,
    identity: dict[str, Any],
    *,
    lock_fd: int | None = None,
) -> None:
    attempt = runtime_dir / "attempts" / uuid.uuid4().hex
    attempt.mkdir(parents=True, mode=0o700)
    venv = attempt / "venv"
    log_path = attempt / "install.log"
    log_path.touch(mode=0o600)
    active = {"schema_version": "ac.runtime_attempt.v1", "attempt": str(attempt), "state": "installing", "started_at": time.time()}
    _atomic_json(attempt / "state.json", active)
    _atomic_json(runtime_dir / "install.active", active)
    requirements = _requirements(lock, mode, roots) + list(identity.get("extra_requirements", ()))
    uv = os.environ.get("AC_INSTALL_UV") or shutil.which("uv")
    try:
        env, paths = _installation_environment(runtime_dir)
        _prepare_install_paths(paths)
        if uv:
            _run_logged([uv, "venv", str(venv), "--python", sys.executable], log_path, env=env, lock_fd=lock_fd)
            command = [uv, "pip", "install", "--python", str(_venv_python(attempt))]
        else:
            _run_logged([sys.executable, "-m", "venv", str(venv)], log_path, env=env, lock_fd=lock_fd)
            command = [str(_venv_python(attempt)), "-m", "pip", "install"]
        if constraints_path.is_file():
            command.extend(["--constraint", str(constraints_path)])
        command.extend(requirements)
        _run_logged(command, log_path, env=env, lock_fd=lock_fd)
        if not _venv_python(attempt).is_file() or any(not _venv_tool(attempt, tool).is_file() for tool in lock.tools):
            raise RuntimeError("installed runtime lacks Python or a required command")
        # Never move an installed venv: its scripts contain absolute shebangs.
        link = runtime_dir / (".venv-" + attempt.name)
        link.symlink_to(venv, target_is_directory=True)
        os.replace(link, runtime_dir / "venv")
        _atomic_json(runtime_dir / "install.ok", {
            "schema_version": "ac.runtime_install.v2", "fingerprint": fingerprint,
            "identity": identity, "venv": str(venv), "installed_at": time.time(), "log": str(log_path),
        })
        _atomic_json(attempt / "state.json", {**active, "state": "completed", "completed_at": time.time()})
        (runtime_dir / "install.failed").unlink(missing_ok=True)
        (runtime_dir / "install.active").unlink(missing_ok=True)
    except BaseException as exc:
        failure = {**active, "state": "failed" if isinstance(exc, Exception) else "interrupted",
                   "error": f"{type(exc).__name__}: {exc}", "log": str(log_path), "cause": "installer_exception; caller cancellation intent unknown"}
        with log_path.open("a", encoding="utf-8") as log:
            log.write(failure["error"] + "\n")
        _atomic_json(attempt / "state.json", failure)
        _atomic_json(runtime_dir / "install.failed", failure)
        raise


def _ready(runtime_dir: Path, fingerprint: str, tools: tuple[str, ...]) -> bool:
    marker = _read_json(runtime_dir / "install.ok")
    try:
        return (marker.get("schema_version") == "ac.runtime_install.v2"
                and marker.get("fingerprint") == fingerprint
                and (runtime_dir / "venv").is_symlink()
                and str((runtime_dir / "venv").readlink()) == marker.get("venv")
                and _venv_python(runtime_dir).is_file()
                and all(_venv_tool(runtime_dir, tool).is_file() for tool in tools))
    except OSError:
        return False


def _ensure_runtime(
    runtime_dir: Path,
    lock: RuntimeLock,
    mode: str,
    roots: dict[str, Path] | None,
    constraints_path: Path,
    fingerprint: str,
    identity: dict[str, Any],
    *,
    retry: bool,
) -> None:
    if _ready(runtime_dir, fingerprint, lock.tools):
        return
    try:
        _prepare_install_paths({"runtime": {"path": str(runtime_dir), "active": True}})
    except RuntimeConfigError as exc:
        _die(str(exc), 1)
    failure = runtime_dir / "install.failed"
    if failure.exists() and not retry:
        _die(
            f"previous runtime install failed: {failure}; rerun setup --retry after fixing cause",
            1,
        )
    with InstallLock(runtime_dir / "install.lock") as ownership:
        if _ready(runtime_dir, fingerprint, lock.tools):
            return
        if failure.exists() and not retry:
            _die(f"previous runtime install failed: {failure}", 1)
        try:
            previous = _read_json(runtime_dir / "install.active")
            if previous:
                _diagnostic("install_attempt_abandoned", previous=previous, cause="interruption_unknown; previous attempt preserved")
            _install(
                runtime_dir,
                lock,
                mode,
                roots,
                constraints_path,
                fingerprint,
                identity,
                lock_fd=ownership.fd,
            )
        except Exception as exc:
            _die(f"runtime install failed: {exc}; see {runtime_dir / 'install.failed'} and retained attempt logs", 1)


def _parser(launcher: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=launcher)
    parser.add_argument("--requirements", type=Path, help="explicit exact optional pins; creates a separate runtime fingerprint")
    subparsers = parser.add_subparsers(dest="operation")
    setup = subparsers.add_parser("setup", help="install or verify private runtime")
    setup.add_argument("--retry", action="store_true")
    subparsers.add_parser("doctor", help="print runtime identity and readiness")
    run = subparsers.add_parser("run", help="run one locked command")
    run.add_argument("tool")
    run.add_argument("args", nargs=argparse.REMAINDER)
    script = subparsers.add_parser(
        "script", help="run one Python script inside the private runtime"
    )
    script.add_argument("path")
    script.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _python_script_command(
    runtime_dir: Path, raw_path: str, args: list[str]
) -> tuple[Path, list[str]]:
    script = Path(raw_path).expanduser().resolve()
    if not script.is_file():
        raise RuntimeConfigError(f"Python script does not exist: {script}")
    python = _venv_python(runtime_dir)
    return python, [str(python), str(script), *args]


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    lock_path = Path(
        os.environ.get("AC_RUNTIME_SOURCES_FILE", script_dir / "runtime-sources.json")
    ).expanduser().resolve()
    constraints_path = Path(
        os.environ.get("AC_RUNTIME_CONSTRAINTS_FILE", script_dir / "runtime-constraints.txt")
    ).expanduser().resolve()
    try:
        lock = load_lock(lock_path)
        launcher = os.environ.get("AC_RUNTIME_LAUNCHER_NAME", "ac-runtime")
        arguments = list(sys.argv[1:] if argv is None else argv)
        if arguments and arguments[0] in lock.tools:
            arguments = ["run", *arguments]
        parser = _parser(launcher)
        namespace = parser.parse_args(arguments)
        extra_requirements = load_extra_requirements(namespace.requirements, lock)
        mode, roots = _source_selection(lock)
        environment = _runtime_environment(lock, roots)
        fingerprint, identity = _fingerprint(
            lock_path, lock, mode, roots, constraints_path, extra_requirements
        )
    except RuntimeConfigError as exc:
        _die(str(exc))
    runtime_dir = (
        Path(environment["AC_RUNTIME_HOME"])
        / f"v{LAUNCHER_VERSION}"
        / lock.profile
        / fingerprint
    )
    retry = bool(getattr(namespace, "retry", False)) or os.environ.get(
        "AC_INSTALL_RETRY", "0"
    ).lower() in {"1", "true", "yes", "on"}
    if namespace.operation == "doctor":
        path_error = None
        try:
            _install_env, paths = _installation_environment(runtime_dir)
        except RuntimeConfigError as exc:
            paths, path_error = {}, str(exc)
        document = {
            "schema_version": "ac.runtime_doctor.v1",
            "path_configuration_error": path_error,
            "install_paths": {name: {**item, **_path_diagnostic(Path(item["path"]))} for name, item in paths.items()},
            "path_check": "read_only_estimate; setup performs actual write probes",
            "lock": {"mechanism": "POSIX flock", "owner_record": _read_json(runtime_dir / "install.owner.json"), "probe": _lock_diagnostic(runtime_dir / "install.lock")},
            "last_attempt": _read_json(runtime_dir / "install.active"),
            "last_failure": _read_json(runtime_dir / "install.failed"),
            "profile": lock.profile,
            "source_mode": mode,
            "fingerprint": fingerprint,
            "runtime": str(runtime_dir),
            "ready": _ready(runtime_dir, fingerprint, lock.tools),
            "environment": environment,
            "sources": identity["sources"],
            "extra_requirements": list(extra_requirements),
        }
        print(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if document["ready"] and path_error is None else 1
    if namespace.operation is None:
        parser.print_usage(sys.stderr)
        return 64
    _ensure_runtime(
        runtime_dir,
        lock,
        mode,
        roots,
        constraints_path,
        fingerprint,
        identity,
        retry=retry,
    )
    if namespace.operation == "setup":
        print(f"{lock.profile} runtime ready: {runtime_dir}")
        return 0
    if namespace.operation == "script":
        try:
            executable, command = _python_script_command(
                runtime_dir, namespace.path, namespace.args
            )
        except RuntimeConfigError as exc:
            _die(str(exc), 64)
        os.execve(str(executable), command, _private_runtime_environment(runtime_dir))
        raise AssertionError("unreachable")
    if namespace.tool not in lock.tools:
        _die(f"command is not present in source lock: {namespace.tool}", 64)
    os.execve(
        str(_venv_tool(runtime_dir, namespace.tool)),
        [namespace.tool, *namespace.args],
        _private_runtime_environment(runtime_dir),
    )
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
