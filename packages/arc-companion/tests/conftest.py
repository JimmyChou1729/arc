from __future__ import annotations

from pathlib import Path
import sys


PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
ARC_LLM_SRC = Path(__file__).resolve().parents[2] / "arc-llm" / "src"
ARC_PAPER_SRC = Path(__file__).resolve().parents[2] / "arc-paper" / "src"
ARC_JOBS_SRC = Path(__file__).resolve().parents[2] / "arc-jobs" / "src"
ARC_TRANSLATE_SRC = (
    Path(__file__).resolve().parents[2] / "arc-translate" / "src"
)
for path in (
    PACKAGE_SRC,
    ARC_LLM_SRC,
    ARC_PAPER_SRC,
    ARC_JOBS_SRC,
    ARC_TRANSLATE_SRC,
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
