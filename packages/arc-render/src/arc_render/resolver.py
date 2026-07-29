"""Deterministic resolution of immutable linear fragment histories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .contracts import FragmentRevision
from .markdown import (
    parse_fragment_revision_filename,
    read_fragment_revision,
)


@dataclass(frozen=True)
class RevisionDiagnostic:
    """One stable, non-destructive history-resolution finding."""

    code: str
    message: str
    severity: str = "warning"
    revision: int | None = None
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in {"warning", "conflict"}:
            raise ValueError("diagnostic severity must be warning or conflict")
        if not self.code or not self.message:
            raise ValueError("diagnostic code and message are required")
        if self.revision is not None and (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("diagnostic revision must be positive")
        object.__setattr__(self, "paths", tuple(self.paths))


@dataclass(frozen=True)
class RevisionResolution:
    """The latest unambiguous revision and all recovery diagnostics."""

    fragment_id: str | None
    selected: FragmentRevision | None
    diagnostics: tuple[RevisionDiagnostic, ...]
    revisions: tuple[FragmentRevision, ...]

    @property
    def selected_digest(self) -> str | None:
        return (
            None
            if self.selected is None
            else self.selected.semantic_digest
        )

    @property
    def has_conflict(self) -> bool:
        return any(
            item.severity == "conflict" for item in self.diagnostics
        )


def resolve_fragment_revisions(
    revisions: Iterable[FragmentRevision],
) -> RevisionResolution:
    """Resolve one fragment graph without choosing a fork winner.

    A child is eligible only when its parent digest exists, its revision is the
    parent's revision plus one, and its immutable source and anchor agree with
    the parent. Multiple children of the selected chain fall back to their
    common parent.
    """

    return _resolve(tuple(revisions), (), fragment_id=None)


def resolve_fragment_revision_files(
    paths: Iterable[str | Path], *, fragment_id: str | None = None
) -> RevisionResolution:
    """Decode and resolve one history, recovering from malformed files."""

    revisions: list[FragmentRevision] = []
    diagnostics: list[RevisionDiagnostic] = []
    for raw_path in paths:
        path = Path(raw_path)
        claimed_revision: int | None = None
        try:
            claimed_revision, _ = parse_fragment_revision_filename(path.name)
        except ValueError:
            diagnostics.append(
                RevisionDiagnostic(
                    code="malformed_revision_filename",
                    message=(
                        "ignored a file whose name is not a fragment "
                        "revision filename"
                    ),
                    paths=(str(path),),
                )
            )
            continue
        try:
            revision = read_fragment_revision(path)
        except ValueError as exc:
            diagnostics.append(
                RevisionDiagnostic(
                    code="malformed_revision",
                    message=f"ignored malformed fragment revision: {exc}",
                    revision=claimed_revision,
                    paths=(str(path),),
                )
            )
            continue
        if fragment_id is not None and revision.fragment_id != fragment_id:
            diagnostics.append(
                RevisionDiagnostic(
                    code="foreign_fragment",
                    message="ignored a revision for another fragment",
                    revision=revision.revision,
                    paths=(str(path),),
                )
            )
            continue
        revisions.append(revision)
    return _resolve(
        tuple(revisions), tuple(diagnostics), fragment_id=fragment_id
    )


def _resolve(
    revisions: tuple[FragmentRevision, ...],
    initial_diagnostics: tuple[RevisionDiagnostic, ...],
    *,
    fragment_id: str | None,
) -> RevisionResolution:
    if any(not isinstance(item, FragmentRevision) for item in revisions):
        raise ValueError("revisions must contain FragmentRevision values")
    fragment_ids = {item.fragment_id for item in revisions}
    if fragment_id is not None:
        if any(item != fragment_id for item in fragment_ids):
            raise ValueError("revisions contain a foreign fragment")
        resolved_id = fragment_id
    elif len(fragment_ids) > 1:
        raise ValueError("revision resolver accepts exactly one fragment")
    else:
        resolved_id = next(iter(fragment_ids), None)

    diagnostics = list(initial_diagnostics)
    by_digest: dict[str, FragmentRevision] = {}
    for revision in revisions:
        existing = by_digest.get(revision.semantic_digest)
        if existing is None:
            by_digest[revision.semantic_digest] = revision
        elif existing != revision:
            # Cryptographic collision or inconsistent object behavior: do not
            # guess, even though this is not expected in normal operation.
            diagnostics.append(
                RevisionDiagnostic(
                    code="semantic_digest_collision",
                    message="two distinct revisions share a semantic digest",
                    severity="conflict",
                    revision=revision.revision,
                )
            )

    unique_revisions = tuple(
        sorted(
            by_digest.values(),
            key=lambda item: (item.revision, item.semantic_digest),
        )
    )
    roots = [item for item in unique_revisions if item.revision == 1]
    if not roots:
        for item in unique_revisions:
            diagnostics.append(
                RevisionDiagnostic(
                    code="dangling_revision",
                    message="revision has no valid initial ancestor",
                    revision=item.revision,
                )
            )
        if not unique_revisions and not diagnostics:
            diagnostics.append(
                RevisionDiagnostic(
                    code="no_usable_revision",
                    message="no fragment revision was supplied",
                )
            )
        return RevisionResolution(
            resolved_id, None, tuple(diagnostics), unique_revisions
        )
    if len(roots) > 1:
        diagnostics.append(
            RevisionDiagnostic(
                code="fork_without_common_parent",
                message=(
                    "multiple initial revisions have no common parent; "
                    "no revision was selected"
                ),
                severity="conflict",
                revision=1,
            )
        )
        return RevisionResolution(
            resolved_id, None, tuple(diagnostics), unique_revisions
        )

    root = roots[0]
    eligible_children: dict[str, list[FragmentRevision]] = defaultdict(list)
    invalid_digests: set[str] = set()
    for item in unique_revisions:
        if item.revision == 1:
            continue
        parent = by_digest.get(item.parent_semantic_digest or "")
        if parent is None:
            diagnostics.append(
                RevisionDiagnostic(
                    code="dangling_revision",
                    message="revision refers to an unavailable parent",
                    revision=item.revision,
                )
            )
            invalid_digests.add(item.semantic_digest)
            continue
        if item.revision != parent.revision + 1:
            diagnostics.append(
                RevisionDiagnostic(
                    code="nonlinear_revision",
                    message="revision number is not its parent's successor",
                    revision=item.revision,
                )
            )
            invalid_digests.add(item.semantic_digest)
            continue
        if item.source != parent.source or item.anchor != parent.anchor:
            diagnostics.append(
                RevisionDiagnostic(
                    code="lineage_identity_changed",
                    message=(
                        "revision changed immutable source or anchor identity"
                    ),
                    revision=item.revision,
                )
            )
            invalid_digests.add(item.semantic_digest)
            continue
        eligible_children[parent.semantic_digest].append(item)

    selected = root
    while True:
        children = [
            item
            for item in eligible_children.get(selected.semantic_digest, ())
            if item.semantic_digest not in invalid_digests
        ]
        if not children:
            break
        if len(children) > 1:
            diagnostics.append(
                RevisionDiagnostic(
                    code="revision_fork",
                    message=(
                        "multiple child revisions conflict; selected their "
                        "common parent"
                    ),
                    severity="conflict",
                    revision=selected.revision + 1,
                )
            )
            break
        selected = children[0]

    return RevisionResolution(
        resolved_id, selected, tuple(diagnostics), unique_revisions
    )


__all__ = [
    "RevisionDiagnostic",
    "RevisionResolution",
    "resolve_fragment_revision_files",
    "resolve_fragment_revisions",
]
