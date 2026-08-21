from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path


sys.dont_write_bytecode = True

FOUNDATION_MODULES = (
    ("ac-jobs", "ac_jobs"),
    ("ac-llm", "ac_llm"),
    ("ac-document", "ac_document"),
    ("ac-proposer-reviewer", "ac_proposer_reviewer"),
)
ARC_MODULES = (("arc-paper", "arc_paper"), ("arc-domain", "arc_domain"))
ARC_REQUIRE_REPO_ROOT = "ARC_REQUIRE_REPO_ROOT"


def bootstrap_arc_pythonpath() -> None:
    """Prefer complete source checkouts; otherwise use the active environment."""
    required = os.environ.get(ARC_REQUIRE_REPO_ROOT)
    if required:
        arc_root = Path(required).expanduser().resolve(strict=True)
        sources = _source_roots(arc_root, strict=True)
        _activate(sources)
        return

    arc_root = _checkout_containing_bootstrap()
    if arc_root is not None:
        sources = _source_roots(arc_root, strict=False)
        if sources is not None:
            _activate(sources)
            return

    for _package, module_name in (*FOUNDATION_MODULES, *ARC_MODULES):
        importlib.import_module(module_name)


def _source_roots(arc_root: Path, *, strict: bool) -> list[tuple[str, Path]] | None:
    foundation_value = os.environ.get("AC_FOUNDATION_REPO_ROOT")
    foundation_root = (
        Path(foundation_value).expanduser().resolve()
        if foundation_value
        else arc_root.parent / "ac-foundation"
    )
    sources = [
        (module, foundation_root / "packages" / package / "src")
        for package, module in FOUNDATION_MODULES
    ]
    sources.extend(
        (module, arc_root / "packages" / package / "src")
        for package, module in ARC_MODULES
    )
    missing = [source / module for module, source in sources if not (source / module).is_dir()]
    if missing and not strict:
        return None
    if missing:
        detail = "\n".join(f"  - {path}" for path in missing)
        raise RuntimeError(f"ARC source mode is missing package modules:\n{detail}")
    return sources


def _activate(sources: list[tuple[str, Path]]) -> None:
    for module_name, source in sources:
        loaded = sys.modules.get(module_name)
        if loaded is not None:
            _assert_module_in_source(loaded, source)
    resolved = [str(source.resolve()) for _module, source in sources]
    source_set = set(resolved)
    sys.path[:] = [entry for entry in sys.path if _resolved_path(entry) not in source_set]
    sys.path[:0] = resolved
    importlib.invalidate_caches()
    for module_name, source in sources:
        _assert_module_in_source(importlib.import_module(module_name), source)


def _assert_module_in_source(module: object, source: Path) -> None:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise RuntimeError(f"Cannot verify module origin for {module!r}")
    try:
        Path(module_file).resolve().relative_to(source.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"ARC source mode rejected {module_file}; expected a module below {source}"
        ) from exc


def _resolved_path(value: str) -> str:
    if not value:
        return value
    try:
        return str(Path(value).resolve())
    except OSError:
        return value


def _checkout_containing_bootstrap() -> Path | None:
    here = Path(__file__).resolve()
    relative = Path(
        "plugins/arc/skills/arc/scripts/_arc_workflows/_arc_script_bootstrap.py"
    )
    for candidate in here.parents:
        try:
            if (candidate / relative).resolve(strict=True) == here:
                return candidate
        except OSError:
            continue
    return None
