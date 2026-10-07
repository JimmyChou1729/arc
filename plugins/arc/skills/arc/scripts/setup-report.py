#!/usr/bin/env python3
"""Explicitly provision verified report fonts in a caller-owned directory."""
import argparse
import json
import shutil
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", help="font profile directory; required when installing")
    parser.add_argument("--tex-dir", help="explicitly install pinned portable TeX in this new owned directory (Linux x86_64/glibc)")
    parser.add_argument("--status", action="store_true", help="read durable TeX operation status; installs nothing")
    parser.add_argument("--retry", action="store_true", help="explicitly recover an interrupted/failed managed TeX operation")
    parser.add_argument("--tex-archive", help="optional existing archive; exact locked SHA-256 is required")
    args = parser.parse_args(argv)
    if args.status:
        if not args.tex_dir or args.retry or args.tex_archive:
            parser.error("--status requires --tex-dir and cannot be combined with --retry/--tex-archive")
    elif not args.output_dir:
        parser.error("--output-dir is required for installation")
    if (args.retry or args.tex_archive) and not args.tex_dir:
        parser.error("--retry/--tex-archive require --tex-dir")
    try:
        bootstrap_arc_pythonpath()
        from _arc_workflows.report_environment import setup_report_environment
        from _arc_workflows.report_delivery import report_dependencies
        if args.status:
            from _arc_workflows.report_tex import report_tex_status
            tex_status = report_tex_status(args.tex_dir)
            profile = None if args.output_dir is None else Path(args.output_dir).expanduser().absolute() / "report-environment.json"
            print(json.dumps({"schema_version": "arc.report_setup_status.v1", "status": "queried", "tex": tex_status,
                              "profile": {"path": None if profile is None else str(profile),
                                          "present": None if profile is None else profile.is_file(), "verification": "not_rechecked"}}, ensure_ascii=False))
            return 1 if tex_status["state"] == "unverifiable" else 0
        tools = None
        tex = None
        if args.tex_dir:
            if shutil.which("pandoc") is None:
                raise RuntimeError("Install Pandoc before explicit owned TeX setup.")
            from _arc_workflows.report_tex import setup_report_tex
            tex = setup_report_tex(args.tex_dir, retry=args.retry, archive=args.tex_archive)
            tools = {"pandoc": shutil.which("pandoc"), **{name: str(Path(tex["bin"]) / name) for name in ("xelatex", "kpsewhich")}}
        result = setup_report_environment(args.output_dir, tools=tools, tex_root=args.tex_dir)
        if tex is not None:
            result["tex"] = tex
        result["diagnostics"] = report_dependencies(environment=result["environment"])
        if result["diagnostics"]["status"] != "available":
            result["status"] = "prerequisites_missing"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "ready" else 1
    except Exception as exc:
        code, status, exit_code = "report_setup_unavailable", "unavailable", 1
        try:
            from ac_jobs import RunBusyError, InstallationOwnershipError, InstallationRetryRequiredError
        except ImportError:
            RunBusyError = InstallationOwnershipError = InstallationRetryRequiredError = ()
        if isinstance(exc, ImportError):
            code = "report_runtime_unavailable"
        elif isinstance(exc, RunBusyError):
            code, status, exit_code = "installation_busy", "running", 75
        elif isinstance(exc, InstallationOwnershipError):
            code, status, exit_code = "installation_ownership_unverifiable", "unverifiable", 75
        elif isinstance(exc, InstallationRetryRequiredError):
            code, status = "installation_retry_required", "retry_required"
        result = {"status": status, "code": code, "error_type": type(exc).__name__, "message": str(exc)}
        if code == "report_runtime_unavailable":
            result["guidance"] = "Initialize the pinned Foundation runtime with arc-runtime setup, then use arc-runtime script <skill-dir>/scripts/setup-report.py."
        if args.tex_dir:
            try:
                from _arc_workflows.report_tex import report_tex_status
                result["tex"] = report_tex_status(args.tex_dir)
            except Exception as query_error:
                result["tex"] = {"state": "unverifiable", "error_type": type(query_error).__name__}
        print(json.dumps(result, ensure_ascii=False))
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
