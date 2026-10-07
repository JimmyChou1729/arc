#!/usr/bin/env python3
"""Explicitly provision verified report fonts in a caller-owned directory."""
import argparse
import json
import shutil
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath
bootstrap_arc_pythonpath()
from _arc_workflows.report_environment import setup_report_environment
from _arc_workflows.report_delivery import report_dependencies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tex-dir", help="explicitly install pinned portable TeX in this new owned directory (Linux x86_64/glibc)")
    args = parser.parse_args(argv)
    try:
        tools = None
        tex = None
        if args.tex_dir:
            if shutil.which("pandoc") is None:
                raise RuntimeError("Install Pandoc before explicit owned TeX setup.")
            from _arc_workflows.report_tex import setup_report_tex
            tex = setup_report_tex(args.tex_dir)
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
        print(json.dumps({"status": "unavailable", "error_type": type(exc).__name__, "message": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
