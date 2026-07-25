"""Paper-centric administration over package-owned cache objects."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ._cache_root import resolve_cache_root
from ._durable_io import atomic_write_bytes
from ._file_lock import exclusive_file_lock
from .ids import normalize_paper_id
from .providers.remote_cache import RemoteCacheAdminEntry, RemoteRequestCache
from .sources import SourceArtifact


CACHE_INDEX_SCHEMA = "arc.paper.cache_index.v1"
_INDEX_FIELDS = {
    "schema_version",
    "entry_id",
    "kind",
    "paper_id",
    "local_source_identity",
    "components",
}
_COMPONENT_FIELDS = {
    "cached_at",
    "time_basis",
    "storage_entry_ids",
}


@dataclass(frozen=True)
class CacheComponent:
    name: str
    cached_at: str
    time_basis: str
    storage_entry_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheEntry:
    entry_id: str
    kind: str
    paper_id: str | None
    local_source_identity: Mapping[str, Any] | None
    components: tuple[CacheComponent, ...]
    cached_at: str
    updateable: bool


@dataclass(frozen=True)
class CacheListResult:
    as_of: str
    since_seconds: int | None
    threshold_at: str | None
    entries: tuple[CacheEntry, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheRemoveResult:
    dry_run: bool
    selected: tuple[CacheEntry, ...]
    removed_entry_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheUpdateRecord:
    entry_id: str
    component: str
    status: str
    message: str = ""


@dataclass(frozen=True)
class CacheUpdateResult:
    records: tuple[CacheUpdateRecord, ...]
    warnings: tuple[str, ...] = ()


class PaperCacheIndex:
    """Atomic logical index; never a content or durable-run identity."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = resolve_cache_root(root)

    def record_paper_component(
        self,
        paper_id: str,
        component: str,
        *,
        cached_at: str,
        time_basis: str,
        storage_entry_ids: Sequence[str] = (),
    ) -> CacheEntry:
        normalized = normalize_paper_id(paper_id)
        if not normalized:
            raise ValueError("paper_id is required")
        entry_id = f"paper:{normalized}"
        return self._record_component(
            entry_id,
            kind="paper",
            paper_id=normalized,
            local_source_identity=None,
            component=component,
            cached_at=cached_at,
            time_basis=time_basis,
            storage_entry_ids=storage_entry_ids,
        )

    def record_local(
        self,
        source: SourceArtifact,
        *,
        cached_at: str,
        time_basis: str,
    ) -> CacheEntry:
        identity = {
            "source_format": source.source_format.value,
            "media_type": source.media_type,
            "artifact_digest": source.artifact_digest,
            "size": source.size,
        }
        entry_id = (
            f"local:{source.source_format.value}:{source.artifact_digest}"
        )
        return self._record_component(
            entry_id,
            kind="local",
            paper_id=None,
            local_source_identity=identity,
            component=f"full-text:{source.source_format.value}",
            cached_at=cached_at,
            time_basis=time_basis,
        )

    def entries(self) -> tuple[CacheEntry, ...]:
        entries_root = self.root / "cache-admin" / "v1" / "entries"
        if not entries_root.is_dir():
            return ()
        entries: list[CacheEntry] = []
        for path in entries_root.glob("*/*/entry.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                entry = _entry_from_document(value)
                if self._entry_path(entry.entry_id) != path:
                    continue
                entries.append(entry)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return tuple(sorted(entries, key=lambda item: item.entry_id))

    def remove(self, entry_id: str) -> bool:
        path = self._entry_path(entry_id)
        with self._entry_lock(entry_id):
            if not path.parent.exists():
                return False
            shutil.rmtree(path.parent)
            return True

    def _record_component(
        self,
        entry_id: str,
        *,
        kind: str,
        paper_id: str | None,
        local_source_identity: Mapping[str, Any] | None,
        component: str,
        cached_at: str,
        time_basis: str,
        storage_entry_ids: Sequence[str] = (),
    ) -> CacheEntry:
        if not component.strip():
            raise ValueError("cache component is required")
        if time_basis not in {
            "recorded_utc",
            "legacy_file_mtime",
            "unreadable",
        }:
            raise ValueError("cache component time_basis is invalid")
        _parse_utc(cached_at)
        path = self._entry_path(entry_id)
        with self._entry_lock(entry_id):
            previous: CacheEntry | None = None
            if path.is_file():
                try:
                    previous = _entry_from_document(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ):
                    previous = None
            by_name = {
                item.name: item for item in previous.components
            } if previous is not None else {}
            existing = by_name.get(component)
            ids = set(storage_entry_ids)
            if existing is not None:
                ids.update(existing.storage_entry_ids)
                if _parse_utc(existing.cached_at) > _parse_utc(cached_at):
                    cached_at = existing.cached_at
                    time_basis = existing.time_basis
            by_name[component] = CacheComponent(
                component,
                cached_at,
                time_basis,
                tuple(sorted(ids)),
            )
            entry = _make_entry(
                entry_id=entry_id,
                kind=kind,
                paper_id=paper_id,
                local_source_identity=local_source_identity,
                components=tuple(by_name.values()),
            )
            atomic_write_bytes(path, _canonical_json_bytes(_entry_document(entry)))
            return entry

    def _entry_path(self, entry_id: str) -> Path:
        digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
        return (
            self.root
            / "cache-admin"
            / "v1"
            / "entries"
            / digest[:2]
            / digest
            / "entry.json"
        )

    @contextmanager
    def _entry_lock(self, entry_id: str) -> Iterator[None]:
        digest = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
        with exclusive_file_lock(
            self.root / "cache-admin" / "v1" / "locks" / f"{digest}.lock"
        ):
            yield


class CacheAdministrator:
    def __init__(self, root: str | Path | None = None) -> None:
        from ._full_text_catalog import FullTextCatalog

        self.root = resolve_cache_root(root)
        self.index = PaperCacheIndex(self.root)
        self.remote = RemoteRequestCache(self.root)
        self.catalog = FullTextCatalog(self.root)

    def list(
        self,
        *,
        paper_ids: Sequence[str] = (),
        entry_ids: Sequence[str] = (),
        since_seconds: int | None = None,
        now: datetime | None = None,
    ) -> CacheListResult:
        if since_seconds is not None and (
            isinstance(since_seconds, bool)
            or not isinstance(since_seconds, int)
            or since_seconds <= 0
        ):
            raise ValueError("since_seconds must be a positive integer")
        as_of = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        threshold = (
            as_of - timedelta(seconds=since_seconds)
            if since_seconds is not None
            else None
        )
        merged = {item.entry_id: item for item in self.index.entries()}
        claimed_remote_ids = {
            storage_id
            for entry in merged.values()
            for component in entry.components
            for storage_id in component.storage_entry_ids
        }

        for item in self.catalog.admin_entries():
            merged[item.entry_id] = _merge_component(
                merged.get(item.entry_id),
                entry_id=item.entry_id,
                kind="paper" if item.entry.kind == "arxiv" else "local",
                paper_id=item.entry.paper_ids[0] if item.entry.paper_ids else None,
                local_source_identity=item.entry.local_source_identity,
                component=CacheComponent(
                    "full-text",
                    item.cached_at,
                    item.time_basis,
                    (item.entry_id,),
                ),
            )

        opaque: list[CacheEntry] = []
        for item in self.remote.admin_entries():
            if item.entry_id in claimed_remote_ids:
                continue
            paper_id, component = _paper_component_from_remote(item)
            if paper_id is not None:
                entry_id = f"paper:{paper_id}"
                merged[entry_id] = _merge_component(
                    merged.get(entry_id),
                    entry_id=entry_id,
                    kind="paper",
                    paper_id=paper_id,
                    local_source_identity=None,
                    component=CacheComponent(
                        component,
                        item.cached_at,
                        item.time_basis,
                        (item.entry_id,),
                    ),
                )
                continue
            opaque.append(
                _make_entry(
                    entry_id=item.entry_id,
                    kind="opaque",
                    paper_id=None,
                    local_source_identity=None,
                    components=(
                        CacheComponent(
                            f"remote:{item.namespace}",
                            item.cached_at,
                            item.time_basis,
                            (item.entry_id,),
                        ),
                    ),
                )
            )

        requested_papers = {
            normalized
            for item in paper_ids
            if (normalized := normalize_paper_id(item))
        }
        requested_entries = {str(item) for item in entry_ids if str(item)}
        values = [*merged.values(), *opaque]
        if requested_papers or requested_entries:
            values = [
                item
                for item in values
                if item.paper_id in requested_papers
                or item.entry_id in requested_entries
            ]
        if threshold is not None:
            values = [
                item for item in values if _parse_utc(item.cached_at) >= threshold
            ]
        values.sort(
            key=lambda item: (
                -_parse_utc(item.cached_at).timestamp(),
                item.paper_id is None,
                item.paper_id or "",
                item.entry_id,
            )
        )
        warnings = tuple(
            f"cache_timestamp_unreadable:{item.entry_id}"
            for item in values
            if any(component.time_basis == "unreadable" for component in item.components)
        )
        return CacheListResult(
            as_of=_format_utc(as_of),
            since_seconds=since_seconds,
            threshold_at=_format_utc(threshold) if threshold is not None else None,
            entries=tuple(values),
            warnings=warnings,
        )


def _paper_component_from_remote(
    entry: RemoteCacheAdminEntry,
) -> tuple[str | None, str]:
    if entry.request_key is None:
        return None, ""
    if entry.namespace == "inspire-record":
        paper_id = normalize_paper_id(entry.request_key)
        return (paper_id or None), "inspire-record"
    if entry.namespace in {"ar5iv-html", "arxiv-pdf"}:
        paper_id = normalize_paper_id(f"arXiv:{entry.request_key}")
        return (
            paper_id or None,
            "ar5iv-html" if entry.namespace == "ar5iv-html" else "arxiv-pdf",
        )
    return None, ""


def _merge_component(
    previous: CacheEntry | None,
    *,
    entry_id: str,
    kind: str,
    paper_id: str | None,
    local_source_identity: Mapping[str, Any] | None,
    component: CacheComponent,
) -> CacheEntry:
    by_name = {
        item.name: item for item in previous.components
    } if previous is not None else {}
    existing = by_name.get(component.name)
    if existing is not None:
        storage_ids = tuple(
            sorted(set(existing.storage_entry_ids + component.storage_entry_ids))
        )
        if _parse_utc(existing.cached_at) > _parse_utc(component.cached_at):
            component = CacheComponent(
                component.name,
                existing.cached_at,
                existing.time_basis,
                storage_ids,
            )
        else:
            component = CacheComponent(
                component.name,
                component.cached_at,
                component.time_basis,
                storage_ids,
            )
    by_name[component.name] = component
    return _make_entry(
        entry_id=entry_id,
        kind=previous.kind if previous is not None else kind,
        paper_id=previous.paper_id if previous is not None else paper_id,
        local_source_identity=(
            previous.local_source_identity
            if previous is not None
            else local_source_identity
        ),
        components=tuple(by_name.values()),
    )


def _make_entry(
    *,
    entry_id: str,
    kind: str,
    paper_id: str | None,
    local_source_identity: Mapping[str, Any] | None,
    components: Sequence[CacheComponent],
) -> CacheEntry:
    ordered = tuple(sorted(components, key=lambda item: item.name))
    if not ordered:
        raise ValueError("cache entry requires a component")
    cached_at = max(ordered, key=lambda item: _parse_utc(item.cached_at)).cached_at
    return CacheEntry(
        entry_id=entry_id,
        kind=kind,
        paper_id=paper_id,
        local_source_identity=(
            dict(local_source_identity) if local_source_identity is not None else None
        ),
        components=ordered,
        cached_at=cached_at,
        updateable=kind == "paper",
    )


def _entry_document(entry: CacheEntry) -> dict[str, Any]:
    return {
        "schema_version": CACHE_INDEX_SCHEMA,
        "entry_id": entry.entry_id,
        "kind": entry.kind,
        "paper_id": entry.paper_id,
        "local_source_identity": (
            dict(entry.local_source_identity)
            if entry.local_source_identity is not None
            else None
        ),
        "components": {
            item.name: {
                "cached_at": item.cached_at,
                "time_basis": item.time_basis,
                "storage_entry_ids": list(item.storage_entry_ids),
            }
            for item in entry.components
        },
    }


def _entry_from_document(value: object) -> CacheEntry:
    if not isinstance(value, Mapping) or set(value) != _INDEX_FIELDS:
        raise ValueError("cache index entry has invalid fields")
    if value.get("schema_version") != CACHE_INDEX_SCHEMA:
        raise ValueError("cache index entry has unsupported schema")
    entry_id = value.get("entry_id")
    kind = value.get("kind")
    paper_id = value.get("paper_id")
    local_identity = value.get("local_source_identity")
    components = value.get("components")
    if not isinstance(entry_id, str) or kind not in {"paper", "local"}:
        raise ValueError("cache index identity is invalid")
    if paper_id is not None and not isinstance(paper_id, str):
        raise ValueError("cache index paper_id is invalid")
    if local_identity is not None and not isinstance(local_identity, Mapping):
        raise ValueError("cache index local identity is invalid")
    if not isinstance(components, Mapping) or not components:
        raise ValueError("cache index components are invalid")
    decoded: list[CacheComponent] = []
    for name, document in components.items():
        if (
            not isinstance(name, str)
            or not isinstance(document, Mapping)
            or set(document) != _COMPONENT_FIELDS
            or not isinstance(document.get("cached_at"), str)
            or document.get("time_basis")
            not in {"recorded_utc", "legacy_file_mtime", "unreadable"}
            or not isinstance(document.get("storage_entry_ids"), list)
            or not all(
                isinstance(item, str)
                for item in document["storage_entry_ids"]
            )
        ):
            raise ValueError("cache index component is invalid")
        _parse_utc(document["cached_at"])
        decoded.append(
            CacheComponent(
                name,
                document["cached_at"],
                document["time_basis"],
                tuple(document["storage_entry_ids"]),
            )
        )
    return _make_entry(
        entry_id=entry_id,
        kind=kind,
        paper_id=paper_id,
        local_source_identity=local_identity,
        components=decoded,
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("cache timestamp must include UTC offset")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "CACHE_INDEX_SCHEMA",
    "CacheAdministrator",
    "CacheComponent",
    "CacheEntry",
    "CacheListResult",
    "CacheRemoveResult",
    "CacheUpdateRecord",
    "CacheUpdateResult",
    "PaperCacheIndex",
]
