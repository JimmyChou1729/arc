#!/usr/bin/env python3
"""Explicitly provision verified report fonts in a caller-owned directory."""
import argparse
import json
import sys

sys.dont_write_bytecode = True
from _arc_workflows._arc_script_bootstrap import bootstrap_arc_pythonpath
bootstrap_arc_pythonpath()
from _arc_workflows.report_environment import setup_report_environment
from _arc_workflows.report_delivery import report_dependencies


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = setup_report_environment(args.output_dir)
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
