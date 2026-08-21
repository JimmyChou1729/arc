#!/usr/bin/env python3
"""Verify generated Foundation runtime and DSH copies."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = ROOT / "plugins/arc/skills/arc/scripts"
MANIFEST = SCRIPT_ROOT / "generated-sources.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if document.get("schema_version") != "ac.generated_sources.v1":
        raise SystemExit("unsupported generated source manifest")
    foundation_root_value = os.environ.get("AC_FOUNDATION_REPO_ROOT")
    foundation_root = (
        Path(foundation_root_value).expanduser().resolve()
        if foundation_root_value
        else None
    )
    if foundation_root is not None:
        revision = subprocess.run(
            ["git", "-C", str(foundation_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if revision != document["foundation_commit"]:
            raise SystemExit(
                f"Foundation checkout is {revision}; expected {document['foundation_commit']}"
            )
    for relative, metadata in document["files"].items():
        generated = (SCRIPT_ROOT / relative).resolve()
        expected = metadata["sha256"]
        if digest(generated) != expected:
            raise SystemExit(f"generated file differs from manifest: {generated}")
        if foundation_root is not None:
            source = foundation_root / metadata["source"]
            if digest(source) != expected:
                raise SystemExit(f"Foundation source differs from manifest: {source}")
    print("generated Foundation files verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
