"""Publication of completed domain-build artifacts.

The durable run owns immutable artifacts.  A domain catalog is only a small,
mutable projection that points callers at the latest created run and at the
newest successfully published export generation.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from arc_jobs import (
    AtomicStateStore,
    ArtifactRef,
    CorruptStateError,
    ImmutableArtifactStore,
    InvalidRunIdError,
    JsonValue,
    RevisionConflictError,
    RunNotFoundError,
    RunRepository,
    RunStatus,
    StateConflictError,
    StateContract,
    canonical_json_bytes,
    validate_simple_id,
)

from .contracts import (
    DomainBuildResult,
    decode_domain_build_result,
    encode_domain_build_result,
)
from .paths import DomainPaths, safe_domain_id


DOMAIN_CATALOG_SCHEMA_VERSION = "arc.domain.catalog.v1"
DOMAIN_EXPORT_MANIFEST_SCHEMA_VERSION = "arc.domain.export_manifest.v1"


class DomainPublicationError(RuntimeError):
    """A completed durable run could not be made available as a domain export."""


@dataclass(frozen=True)
class DomainCatalog:
    """The closed mutable state for one domain's published generations."""

    revision: int
    latest: str | None
    active: str | None

    def __post_init__(self) -> None:
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValueError("catalog revision must be a non-negative integer")
        for field_name in ("latest", "active"):
            value = getattr(self, field_name)
            if value is None:
                continue
            _validate_run_id(value, field_name=field_name)


@dataclass(frozen=True)
class DomainCatalogRepairDiagnostic:
    """One operation-scoped repair of an unreadable catalog pointer."""

    pointer: str
    previous_run_id: str
    replacement_run_id: str
    reason: str
    code: str = field(init=False, default="domain_catalog_pointer_repaired")

    def __post_init__(self) -> None:
        if self.pointer not in {"latest", "active"}:
            raise ValueError("catalog repair pointer must be latest or active")
        if self.reason not in {"run_not_found", "corrupt_state"}:
            raise ValueError("catalog repair reason is invalid")
        _validate_run_id(self.previous_run_id, field_name="previous_run_id")
        _validate_run_id(self.replacement_run_id, field_name="replacement_run_id")


@dataclass(frozen=True)
class DomainCatalogUpdate:
    """A catalog mutation plus non-durable diagnostics from that mutation."""

    catalog: DomainCatalog
    diagnostics: tuple[DomainCatalogRepairDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, DomainCatalog):
            raise TypeError("catalog update must contain a DomainCatalog")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, DomainCatalogRepairDiagnostic)
            for item in self.diagnostics
        ):
            raise TypeError("catalog update diagnostics are invalid")

    @property
    def revision(self) -> int:
        """Compatibility projection for callers that previously got a catalog."""

        return self.catalog.revision

    @property
    def latest(self) -> str | None:
        return self.catalog.latest

    @property
    def active(self) -> str | None:
        return self.catalog.active


@dataclass(frozen=True)
class DomainPublication:
    """The result of materializing one run's public domain generation."""

    domain_id: str
    run_id: str
    manifest_path: Path
    active: bool
    catalog: DomainCatalog
    diagnostics: tuple[DomainCatalogRepairDiagnostic, ...] = ()


class _CatalogContract(StateContract[DomainCatalog]):
    schema_version = DOMAIN_CATALOG_SCHEMA_VERSION

    def encode(self, value: DomainCatalog) -> Mapping[str, JsonValue]:
        return {
            "revision": value.revision,
            "latest": value.latest,
            "active": value.active,
        }

    def decode(self, document: Mapping[str, JsonValue]) -> DomainCatalog:
        if set(document) != {"revision", "latest", "active"}:
            raise CorruptStateError("domain catalog uses an invalid closed shape")
        revision = document["revision"]
        latest = document["latest"]
        active = document["active"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise CorruptStateError("domain catalog revision is invalid")
        for field_name, value in (("latest", latest), ("active", active)):
            if value is not None and not isinstance(value, str):
                raise CorruptStateError(f"domain catalog {field_name} is invalid")
            if value is not None:
                try:
                    _validate_run_id(value, field_name=field_name)
                except ValueError as exc:
                    raise CorruptStateError(str(exc)) from exc
        return DomainCatalog(revision=revision, latest=latest, active=active)

    def validate_transition(
        self, previous: DomainCatalog | None, next_value: DomainCatalog
    ) -> None:
        if previous is None:
            if next_value.revision != 0:
                raise CorruptStateError("initial domain catalog revision must be zero")
            return
        if next_value.revision != previous.revision + 1:
            raise CorruptStateError("domain catalog revision must increment by one")


def register_domain_run(
    repository: RunRepository,
    paths: DomainPaths,
    *,
    domain_id: str,
    run_id: str,
) -> DomainCatalogUpdate:
    """Record a newly created run as ``latest`` without changing ``active``.

    The caller should invoke this when the durable run is created.  Comparing
    immutable run creation timestamps also makes a delayed call for an older
    run harmless.
    """

    safe_id = safe_domain_id(domain_id)
    snapshot = repository.inspect(_validated_run_id(run_id)).snapshot
    return _update_catalog(
        repository,
        paths,
        domain_id=safe_id,
        update=lambda current: _with_latest_if_newer(
            current, candidate_run_id=snapshot.run_id, candidate_created_at=snapshot.created_at, repository=repository
        ),
    )


def publish_domain_result(
    repository: RunRepository,
    paths: DomainPaths,
    *,
    run_id: str,
    result: DomainBuildResult,
) -> DomainPublication:
    """Materialize one completed result and conditionally advance ``active``.

    All source refs are verified before any export file is touched.  The export
    manifest is written only after every public file has been materialized.  A
    publication exception therefore leaves the durable run untouched and never
    advances the catalog's active pointer.
    """

    snapshot = repository.inspect(_validated_run_id(run_id)).snapshot
    if snapshot.status is not RunStatus.SUCCEEDED:
        raise DomainPublicationError(
            f"run {snapshot.run_id!r} is not succeeded and cannot be published"
        )
    if snapshot.result_ref is None:
        raise DomainPublicationError(
            f"succeeded run {snapshot.run_id!r} has no result artifact"
        )
    if not isinstance(result, DomainBuildResult):
        raise ValueError("result must be a DomainBuildResult")

    source_store = ImmutableArtifactStore(
        repository.run_directory(snapshot.run_id), repository_root=repository.root
    )
    authoritative_result = _read_run_result(source_store, snapshot.result_ref)
    if result != authoritative_result:
        raise DomainPublicationError(
            "provided DomainBuildResult does not match the succeeded run result artifact"
        )
    safe_id = safe_domain_id(authoritative_result.domain_id)

    # ``latest`` models run creation, independently of whether publication
    # succeeds.  A build handler normally makes this call at run creation; it
    # is repeated here to make standalone repair safe.
    latest_update = register_domain_run(
        repository,
        paths,
        domain_id=safe_id,
        run_id=snapshot.run_id,
    )

    exported = _exported_artifacts(authoritative_result)
    source_bytes = {
        filename: source_store.read_bytes(ref) for filename, ref in exported.items()
    }
    # Foundation selection remains a durable/programmatic artifact.  Validate
    # it with the rest of the result, but do not expose an intermediate file.
    source_store.verify(authoritative_result.foundation_selection)

    generation = paths.export_generation(safe_id, snapshot.run_id)
    for filename, content in source_bytes.items():
        _atomic_write_bytes(generation / filename, content)

    manifest_path = generation / "export-manifest.json"
    manifest = _export_manifest(
        domain_id=safe_id,
        run_id=snapshot.run_id,
        created_at=snapshot.created_at,
        result=authoritative_result,
        exported=exported,
    )
    # This is intentionally the final write to a generation.
    _atomic_write_bytes(manifest_path, canonical_json_bytes(manifest) + b"\n")

    active_update = _update_catalog(
        repository,
        paths,
        domain_id=safe_id,
        update=lambda current: _with_active_if_newer(
            current,
            candidate_run_id=snapshot.run_id,
            candidate_created_at=snapshot.created_at,
            repository=repository,
        ),
    )
    catalog = active_update.catalog
    return DomainPublication(
        domain_id=safe_id,
        run_id=snapshot.run_id,
        manifest_path=manifest_path,
        active=catalog.active == snapshot.run_id,
        catalog=catalog,
        diagnostics=(*latest_update.diagnostics, *active_update.diagnostics),
    )


def read_domain_catalog(paths: DomainPaths, *, domain_id: str) -> DomainCatalog | None:
    """Read the catalog without creating a domain directory."""

    safe_id = safe_domain_id(domain_id)
    return _catalog_store(paths, safe_id).read()


def _exported_artifacts(result: DomainBuildResult) -> dict[str, ArtifactRef]:
    exported: dict[str, ArtifactRef] = {
        "graph.json": result.graph,
        "network.html": result.network_html,
        "paper-pack.json": result.paper_json_pack,
        "evidence-pack.json": result.evidence_pack,
    }
    if result.summary is not None:
        exported["summary.json"] = result.summary
    if result.summary_markdown is not None:
        exported["summary.md"] = result.summary_markdown
    return exported


def _read_run_result(
    artifacts: ImmutableArtifactStore, result_ref: ArtifactRef
) -> DomainBuildResult:
    try:
        document = json.loads(artifacts.read_bytes(result_ref).decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("result artifact must contain an object")
        return decode_domain_build_result(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DomainPublicationError(
            "succeeded run result artifact is not a valid DomainBuildResult"
        ) from exc


def _export_manifest(
    *,
    domain_id: str,
    run_id: str,
    created_at: str,
    result: DomainBuildResult,
    exported: Mapping[str, ArtifactRef],
) -> dict[str, JsonValue]:
    from arc_jobs import encode_artifact_ref

    files: dict[str, JsonValue] = {}
    for filename, value in exported.items():
        files[filename] = encode_artifact_ref(value)
    return {
        "schema_version": DOMAIN_EXPORT_MANIFEST_SCHEMA_VERSION,
        "domain_id": domain_id,
        "run_id": run_id,
        "run_created_at": created_at,
        "result": encode_domain_build_result(result),
        "files": files,
    }


def _catalog_store(paths: DomainPaths, domain_id: str) -> AtomicStateStore[DomainCatalog]:
    return AtomicStateStore(paths.catalog(domain_id), _CatalogContract())


def _update_catalog(
    repository: RunRepository,
    paths: DomainPaths,
    *,
    domain_id: str,
    update: Callable[[DomainCatalog], DomainCatalogUpdate],
) -> DomainCatalogUpdate:
    store = _catalog_store(paths, domain_id)
    while True:
        current = store.read()
        if current is None:
            planned = update(DomainCatalog(revision=0, latest=None, active=None))
            if not isinstance(planned, DomainCatalogUpdate):
                raise TypeError("catalog update must return DomainCatalogUpdate")
            try:
                created = store.create(replace(planned.catalog, revision=0))
                return DomainCatalogUpdate(created, planned.diagnostics)
            except StateConflictError:
                continue
        planned = update(current)
        if not isinstance(planned, DomainCatalogUpdate):
            raise TypeError("catalog update must return DomainCatalogUpdate")
        next_value = planned.catalog
        if next_value == current:
            return DomainCatalogUpdate(current, planned.diagnostics)
        try:
            updated = store.compare_and_swap(current.revision, next_value)
            return DomainCatalogUpdate(updated, planned.diagnostics)
        except RevisionConflictError:
            continue


def _with_latest_if_newer(
    current: DomainCatalog,
    *,
    candidate_run_id: str,
    candidate_created_at: str,
    repository: RunRepository,
) -> DomainCatalogUpdate:
    diagnostic = None
    if current.latest is not None:
        newer, diagnostic = _is_newer_run(
            candidate_run_id,
            candidate_created_at,
            current.latest,
            repository,
            pointer="latest",
        )
        if not newer:
            return DomainCatalogUpdate(current)
    return DomainCatalogUpdate(
        DomainCatalog(
            revision=current.revision
            + (0 if current.latest == candidate_run_id else 1),
            latest=candidate_run_id,
            active=current.active,
        ),
        () if diagnostic is None else (diagnostic,),
    )


def _with_active_if_newer(
    current: DomainCatalog,
    *,
    candidate_run_id: str,
    candidate_created_at: str,
    repository: RunRepository,
) -> DomainCatalogUpdate:
    diagnostic = None
    if current.active is not None:
        newer, diagnostic = _is_newer_run(
            candidate_run_id,
            candidate_created_at,
            current.active,
            repository,
            pointer="active",
        )
        if not newer:
            return DomainCatalogUpdate(current)
    return DomainCatalogUpdate(
        DomainCatalog(
            revision=current.revision
            + (0 if current.active == candidate_run_id else 1),
            latest=current.latest,
            active=candidate_run_id,
        ),
        () if diagnostic is None else (diagnostic,),
    )


def _is_newer_run(
    candidate_run_id: str,
    candidate_created_at: str,
    existing_run_id: str,
    repository: RunRepository,
    *,
    pointer: str,
) -> tuple[bool, DomainCatalogRepairDiagnostic | None]:
    try:
        existing = repository.inspect(existing_run_id).snapshot
    except RunNotFoundError:
        reason = "run_not_found"
    except CorruptStateError:
        reason = "corrupt_state"
    else:
        return (
            (candidate_created_at, candidate_run_id)
            > (existing.created_at, existing.run_id),
            None,
        )
    return (
        True,
        DomainCatalogRepairDiagnostic(
            pointer=pointer,
            previous_run_id=existing_run_id,
            replacement_run_id=candidate_run_id,
            reason=reason,
        ),
    )


def _validated_run_id(value: str) -> str:
    try:
        validate_simple_id(value, label="run id")
    except InvalidRunIdError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _validate_run_id(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"catalog {field_name} must be a run id")
    _validated_run_id(value)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Publish one export file without exposing a partial replacement."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":  # pragma: no cover - Windows
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "DOMAIN_CATALOG_SCHEMA_VERSION",
    "DOMAIN_EXPORT_MANIFEST_SCHEMA_VERSION",
    "DomainCatalog",
    "DomainCatalogRepairDiagnostic",
    "DomainCatalogUpdate",
    "DomainPublication",
    "DomainPublicationError",
    "publish_domain_result",
    "read_domain_catalog",
    "register_domain_run",
]
