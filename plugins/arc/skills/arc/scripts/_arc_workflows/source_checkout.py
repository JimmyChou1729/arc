"""Strict source-checkout path policy shared by ARC Skill workflows."""

from __future__ import annotations

import os
from pathlib import Path


REQUIRED_REPO_ROOT_ENV = "ARC_REQUIRE_REPO_ROOT"


def validate_strict_checkout_path(
    path: str | Path,
    *,
    expected_relative_path: str | Path,
    field_name: str,
    error_type: type[Exception] = ValueError,
) -> None:
    """Require an exact checkout-owned path when strict source mode is active."""

    required_root = str(os.environ.get(REQUIRED_REPO_ROOT_ENV, "")).strip()
    if not required_root:
        return
    root = Path(required_root).expanduser().resolve()
    expected = (root / expected_relative_path).resolve()
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise error_type(
            f"strict ARC source mode cannot resolve {field_name}: {candidate}"
        ) from exc
    if resolved != expected:
        raise error_type(
            f"strict ARC source mode requires {field_name} from the required "
            f"checkout: expected {expected}, got {resolved}"
        )


__all__ = [
    "REQUIRED_REPO_ROOT_ENV",
    "validate_strict_checkout_path",
]
