"""Strict MinerU page-map input adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RUNNING_TYPES = {"header", "footer", "page_number"}


class ProofreadSourceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceAsset:
    source: Path
    delivery_name: str
    sha256: str


@dataclass(frozen=True)
class MineruPage:
    page_index: int
    markdown: str
    source_blocks: tuple[dict[str, Any], ...]
    asset_names: tuple[str, ...]


@dataclass(frozen=True)
class MineruSource:
    markdown_path: Path
    pdf_path: Path
    content_list_path: Path
    markdown_sha256: str
    pdf_sha256: str
    content_list_sha256: str
    pages: tuple[MineruPage, ...]
    assets: tuple[SourceAsset, ...]

    @property
    def page_count(self) -> int:
        return len(self.pages)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_mineru_source(
    markdown: str | Path,
    pdf: str | Path,
    content_list: str | Path,
) -> MineruSource:
    markdown_path = _required_file(markdown, "source_markdown_missing")
    pdf_path = _required_file(pdf, "source_pdf_missing")
    content_path = _required_file(content_list, "page_map_missing")
    try:
        raw = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofreadSourceError("page_map_invalid", "MinerU content list is unreadable") from exc
    if not isinstance(raw, list):
        raise ProofreadSourceError("page_map_invalid", "MinerU content list must be an array")

    grouped: dict[int, list[dict[str, Any]]] = {}
    for ordinal, value in enumerate(raw):
        if not isinstance(value, dict) or type(value.get("page_idx")) is not int:
            raise ProofreadSourceError(
                "page_map_invalid", f"content-list item {ordinal} has no integer page_idx"
            )
        page_index = int(value["page_idx"])
        if page_index < 0:
            raise ProofreadSourceError("page_map_invalid", "page_idx cannot be negative")
        grouped.setdefault(page_index, []).append(dict(value))
    page_count = _pdf_page_count(pdf_path)
    if any(page_index >= page_count for page_index in grouped):
        raise ProofreadSourceError("page_map_out_of_range", "page_idx exceeds PDF page count")
    expected = list(range(page_count))

    assets_by_source: dict[Path, SourceAsset] = {}
    pages: list[MineruPage] = []
    for page_index in expected:
        blocks = grouped.get(page_index, [])
        parts: list[str] = []
        page_assets: list[str] = []
        for block in blocks:
            path = _asset_path(block, content_path.parent)
            delivery_name = None
            if path is not None:
                asset = assets_by_source.get(path)
                if asset is None:
                    asset_digest = sha256_file(path)
                    delivery_name = f"{asset_digest[:12]}-{path.name}"
                    asset = SourceAsset(path, delivery_name, asset_digest)
                    assets_by_source[path] = asset
                delivery_name = asset.delivery_name
                page_assets.append(delivery_name)
            rendered = _item_markdown(block, delivery_name)
            if rendered:
                parts.append(rendered)
        pages.append(
            MineruPage(
                page_index,
                "\n\n".join(parts).strip(),
                tuple(blocks),
                tuple(page_assets),
            )
        )

    return MineruSource(
        markdown_path.resolve(),
        pdf_path.resolve(),
        content_path.resolve(),
        sha256_file(markdown_path),
        sha256_file(pdf_path),
        sha256_file(content_path),
        tuple(pages),
        tuple(sorted(assets_by_source.values(), key=lambda item: item.delivery_name)),
    )


def _required_file(value: str | Path, code: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise ProofreadSourceError(code, f"required input is missing: {path}")
    return path


def _pdf_page_count(path: Path) -> int:
    try:
        completed = subprocess.run(
            ("pdfinfo", str(path)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ProofreadSourceError("pdf_info_unavailable", "PDF page count is unavailable") from exc
    if completed.returncode != 0:
        raise ProofreadSourceError("pdf_invalid", "PDF metadata could not be read")
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            count = int(line.split(":", 1)[1].strip())
            if count > 0:
                return count
    raise ProofreadSourceError("pdf_invalid", "PDF has no positive page count")


def _asset_path(item: dict[str, Any], root: Path) -> Path | None:
    raw = item.get("img_path")
    if not isinstance(raw, str) or not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProofreadSourceError("asset_path_invalid", f"unsafe MinerU asset path: {raw}")
    path = (root / candidate).resolve()
    if not path.is_file():
        raise ProofreadSourceError("asset_missing", f"MinerU asset is missing: {raw}")
    return path


def _item_markdown(item: dict[str, Any], asset_name: str | None) -> str:
    kind = item.get("type")
    if kind in RUNNING_TYPES:
        return ""
    if kind == "text":
        text = str(item.get("text") or "").strip()
        level = item.get("text_level")
        if text and type(level) is int and 1 <= level <= 6:
            return f"{'#' * level} {text}"
        return text
    if kind in {"aside_text", "ref_text", "page_footnote", "equation"}:
        return str(item.get("text") or "").strip()
    if kind == "list":
        values = item.get("list_items")
        if not isinstance(values, list):
            return ""
        return "\n".join(
            value if value[:1].isdigit() else f"- {value}"
            for raw in values
            if (value := str(raw).strip())
        )
    if kind in {"image", "chart", "table"}:
        parts = [f"![](proofread-assets/{asset_name})"] if asset_name else []
        content_key = "table_body" if kind == "table" else "content"
        content = item.get(content_key)
        if isinstance(content, str) and content.strip():
            parts.append(content.strip())
        caption_key = {
            "image": "image_caption",
            "chart": "chart_caption",
            "table": "table_caption",
        }[kind]
        captions = item.get(caption_key)
        if isinstance(captions, list):
            parts.extend(str(value).strip() for value in captions if str(value).strip())
        return "\n\n".join(parts)
    return str(item.get("text") or "").strip()


__all__ = [
    "MineruPage",
    "MineruSource",
    "ProofreadSourceError",
    "SourceAsset",
    "load_mineru_source",
    "sha256_file",
]
