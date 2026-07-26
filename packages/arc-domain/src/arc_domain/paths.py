from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from arc_jobs import InvalidRunIdError, canonical_json_bytes, validate_simple_id
from arc_paper import normalize_paper_id


def domain_id_for(seed_paper: str, intent: str = "") -> str:
    if not isinstance(seed_paper, str):
        raise ValueError("seed_paper must be a string")
    if not isinstance(intent, str):
        raise ValueError("intent must be a string")
    seed_id = normalize_paper_id(seed_paper)
    if not seed_id:
        raise ValueError("seed_paper must resolve to a non-empty paper identifier")
    normalized_intent = intent.strip()
    stem = re.sub(r"[^A-Za-z0-9.]+", "_", seed_id).strip("_") or "domain"
    digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "seed_paper": seed_id,
                "intent": normalized_intent,
            }
        )
    ).hexdigest()[:16]
    return safe_domain_id(f"{stem}_{digest}")


def safe_domain_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("domain_id must be a string")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return safe.strip("._-") or "domain"


@dataclass(frozen=True)
class DomainPaths:
    root: Path

    @classmethod
    def for_project(cls, project_dir: str | Path) -> "DomainPaths":
        """Return the project-owned durable domain state location.

        Domain builds are not reusable caches.  Their durable runs and
        unpublished generations therefore always live inside the selected
        project rather than under a shared ARC root.
        """

        if isinstance(project_dir, str) and not project_dir.strip():
            raise ValueError("project_dir must be a non-empty path")
        project = Path(project_dir).expanduser().resolve(strict=False)
        return cls(project / ".arc" / "domain")

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def domains(self) -> Path:
        return self.root / "domains"

    def domain(self, domain_id: str) -> Path:
        return self.domains / safe_domain_id(domain_id)

    def catalog(self, domain_id: str) -> Path:
        return self.domain(domain_id) / "catalog.json"

    def exports(self, domain_id: str) -> Path:
        return self.domain(domain_id) / "exports"

    def export_generation(self, domain_id: str, run_id: str) -> Path:
        try:
            validate_simple_id(run_id, label="run id")
        except InvalidRunIdError as exc:
            raise ValueError(str(exc)) from exc
        return self.exports(domain_id) / run_id


__all__ = ["DomainPaths", "domain_id_for", "safe_domain_id"]
