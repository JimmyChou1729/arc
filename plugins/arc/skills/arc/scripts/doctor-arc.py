#!/usr/bin/env python3
"""Offline ARC dependency checks, separate from runtime lock readiness."""

from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import json
import sys

sys.dont_write_bytecode = True
from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath
bootstrap_arc_pythonpath()
from _arc_workflows.report_delivery import report_dependencies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", help="existing project to probe write access")
    parser.add_argument("--report-environment", help="explicit report-environment.json")
    args = parser.parse_args(argv)
    try:
        from ac_llm import environment_diagnostics
        environment = environment_diagnostics(project_dir=args.project_dir)
    except ImportError:
        environment = {"status": "foundation_update_required", "guidance": "Use the tested Foundation source override during development; the release lock must include the Host implementation."}
    from ac_llm.providers import default_registry
    providers = {}
    for name in ("codex", "claude", "kimi", "dsh", "host"):
        try:
            diagnostic = default_registry().create(name).doctor()
            providers[name] = {"available": diagnostic.available, "prelaunch_unavailable": getattr(diagnostic, "prelaunch_unavailable", False),
                               "authentication": "not_checked"}
        except Exception as exc:
            providers[name] = {"available": False, "configuration_error": type(exc).__name__}
    scientific_versions = {}
    for name in ("numpy", "scipy", "sympy", "matplotlib"):
        try:
            scientific_versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            scientific_versions[name] = None
    result = {"schema_version": "arc.doctor.v1", "environment": environment, "providers": providers,
              "scientific_python": {"executable": sys.executable, "scope": "current_interpreter_only", "versions": scientific_versions, "imports_checked": False},
              "scientific_libraries": {name: importlib.util.find_spec(name) is not None for name in ("numpy", "scipy", "sympy", "matplotlib")},
              "report": report_dependencies(environment=args.report_environment)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
