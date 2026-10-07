#!/usr/bin/env python3
"""Render and inspect a real CJK/math PDF without invoking a model."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
from _arc_workflows.report_delivery import render_markdown_pdf, ReportDeliveryUnavailable

SAMPLE = r"""# ARC 报告渲染验证

中文、数学与字体检查。ARC report verification.

行内公式：$E=mc^2$，以及 $\sigma(0)=1$。

陈列公式：

$$
\ddot{\sigma}+3H\dot{\sigma}+m^2\sigma=0,
\qquad \int_0^1 x^2\,dx=\frac{1}{3}.
$$

这是一份渲染兼容性样例，不是科学研究结果。
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--environment")
    args = parser.parse_args(argv)
    result = {"schema_version": "arc.report_verification.v1", "executed": False,
              "scientific_accepted": None, "report_delivered": False}
    try:
        project = Path(args.project_dir).expanduser().resolve()
        project.mkdir(parents=True, exist_ok=True)
        for name in ("pdfinfo", "pdftotext"):
            if shutil.which(name) is None:
                raise RuntimeError(f"Verification requires {name}; install Poppler explicitly.")
        source = project / ".arc/report-verification.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(SAMPLE, encoding="utf-8")
        result["executed"] = True
        output = render_markdown_pdf(project_dir=project, source=source, output=project / "report-verification.pdf", environment=args.environment)
        info = subprocess.run(["pdfinfo", str(output)], capture_output=True, text=True, check=True, timeout=30)
        match = re.search(r"(?m)^Pages:\s+(\d+)", info.stdout)
        pages = int(match.group(1)) if match else 0
        extracted = subprocess.run(["pdftotext", str(output), "-"], capture_output=True, text=True, check=True, timeout=30)
        text_ok = "报告渲染验证" in extracted.stdout and "ARC report verification" in extracted.stdout
        if pages < 1 or not text_ok:
            raise RuntimeError("PDF page/text verification failed")
        result.update(report_delivered=True, pages=pages, text_verified=True, pdf=str(output),
                      visual_review="required", message="Render pages with pdftoppm and visually inspect the equations before host acceptance.")
    except (ReportDeliveryUnavailable, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        result.update(error_type=type(exc).__name__, message=str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["report_delivered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
