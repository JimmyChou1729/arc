#!/usr/bin/env python3
"""Inspect this interpreter or run a bounded numerical compatibility fixture."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import platform
import sys

sys.dont_write_bytecode = True


def provenance():
    libraries = {}
    for name in ("numpy", "scipy", "sympy", "matplotlib"):
        available = importlib.util.find_spec(name) is not None
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            version = None
        libraries[name] = {"available": available, "version": version, "import_checked": False}
    return {"executable": sys.executable, "python": platform.python_version(), "platform": platform.platform(),
            "scope": "current_interpreter_only", "libraries": libraries}


def verify(state=None):
    import numpy as np
    import scipy
    from scipy.integrate import solve_ivp

    if state is not None:
        state["executed"] = True
    t = np.linspace(0, 8, 401)
    results = []
    for mu in (0.0, 1.0, 1.5, 2.0):
        if mu == 0:
            expected = np.ones_like(t)
        elif mu == 1.5:
            expected = np.exp(-1.5 * t) * (1 + 1.5 * t)
        elif mu < 1.5:
            d = np.sqrt(2.25 - mu * mu)
            expected = np.exp(-1.5 * t) * (np.cosh(d * t) + 1.5 / d * np.sinh(d * t))
        else:
            w = np.sqrt(mu * mu - 2.25)
            expected = np.exp(-1.5 * t) * (np.cos(w * t) + 1.5 / w * np.sin(w * t))
        methods = []
        for method in ("RK45", "DOP853"):
            solution = solve_ivp(lambda time, y: [y[1], -3 * y[1] - mu * mu * y[0]], (0, 8), (1, 0),
                                 method=method, t_eval=t, rtol=1e-10, atol=1e-12)
            if not solution.success or solution.y.shape != (2, len(t)):
                raise RuntimeError(f"{method} integration failed for mu={mu}")
            error = np.abs(solution.y[0] - expected)
            passed = bool(np.all(error <= 2e-9 + 2e-8 * np.abs(expected)))
            methods.append({"method": method, "passed": passed, "maximum_sampled_absolute_error": float(np.max(error))})
        results.append({"mu": mu, "methods": methods})
    return {"executed": True, "passed": all(m["passed"] for row in results for m in row["methods"]),
            "claim": "finite_sample_consistency_only", "scientific_accepted": None, "report_delivered": None,
            "definition": "mu=m/H; tau=Ht; y''+3y'+mu^2*y=0; y(0)=1, y'(0)=0", "samples": len(t),
            "versions_used": {"numpy": np.__version__, "scipy": scipy.__version__}, "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    result = {"schema_version": "arc.scientific_python.v1", **provenance()}
    if args.verify:
        verification = {"executed": False, "passed": False}
        try:
            result["verification"] = verify(verification)
            for name, version in result["verification"]["versions_used"].items():
                result["libraries"][name].update(version=version, import_checked=True)
        except Exception as exc:
            verification.update(error_type=type(exc).__name__, message=str(exc))
            result["verification"] = verification
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not args.verify or result["verification"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
