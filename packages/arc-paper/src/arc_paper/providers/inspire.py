from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ..ids import arxiv_path_id, doi_value, inspire_recid, normalize_paper_id
from .base import ProviderError
from .remote_cache import RemoteCacheError, RemoteRequestCache


BASE_URL = "https://inspirehep.net/api"
INSPIRE_HOST = "inspirehep.net"
MAX_PAGE_SIZE = 1000
MAX_JSON_BYTES = 50 * 1024 * 1024
MATHML_RE = re.compile(r"<math\b[^>]*>.*?</math>", re.IGNORECASE | re.DOTALL)
HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")
SUMMARY_FIELDS = ",".join(
    [
        "titles",
        "authors",
        "arxiv_eprints",
        "dois",
        "citation_count",
        "earliest_date",
        "preprint_date",
        "publication_info",
        "abstracts",
    ]
)
INSPIRE_CITERS_RAW_PAYLOAD_CONTRACT = "arc.paper.inspire_citers.raw.v1"


@dataclass(frozen=True)
class InspireCiterRequest:
    sort: str
    limit: int
    request_key: str
    admin_component: str


def describe_inspire_citer_request(
    recid: str,
    *,
    sort: str = "mostrecent",
    limit: int = MAX_PAGE_SIZE,
) -> InspireCiterRequest:
    canonical_sort = _normalize_sort(sort)
    canonical_limit = _clamp_limit(limit)
    request_key = json.dumps(
        {
            "contract": INSPIRE_CITERS_RAW_PAYLOAD_CONTRACT,
            "limit": canonical_limit,
            "recid": str(recid),
            "sort": canonical_sort,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return InspireCiterRequest(
        sort=canonical_sort,
        limit=canonical_limit,
        request_key=request_key,
        admin_component=f"inspire-citers:{canonical_sort}:{canonical_limit}",
    )


class InspireProvider:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        cache_root: str | Path | None = None,
        request_cache: RemoteRequestCache | None = None,
    ):
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self.timeout = timeout
        self.cache = request_cache or RemoteRequestCache(cache_root)

    def get_metadata(self, paper_id: str, *, refresh: bool = False) -> dict[str, Any]:
        raw = self.get_raw_record(paper_id, refresh=refresh)
        return _normalize_record(raw)

    def search_metadata(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise ProviderError("inspire_search_query_required", "INSPIRE search requires a query")
        size = _clamp_limit(limit)
        params = {
            "q": normalized_query,
            "size": str(size),
            "sort": "mostcited",
            "fields": SUMMARY_FIELDS,
            "format": "json",
        }
        data = self.cache.fetch_json(
            "inspire-search",
            json.dumps(
                {"query": normalized_query, "size": size},
                sort_keys=True,
                separators=(",", ":"),
            ),
            fetch=lambda: self._request_json(
                f"{BASE_URL}/literature",
                params=params,
                error_code="inspire_search_failed",
            ),
        )
        return [
            _normalize_record(hit)
            for hit in data.get("hits", {}).get("hits", [])[:size]
        ]

    def get_references(self, paper_id: str, *, refresh: bool = False, enrich: bool = False) -> list[dict[str, Any]]:
        raw = self.get_raw_record(paper_id, refresh=refresh)
        references = [
            normalized
            for item in raw.get("metadata", {}).get("references", [])
            if (normalized := _normalize_reference(item))
        ]

        if enrich:
            references = self.enrich_reference_metadata(references, refresh=refresh)
        return references

    def enrich_reference_metadata(
        self,
        references: list[dict[str, Any]],
        *,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for reference in references:
            lookup_id = _reference_lookup_id(reference)
            if not lookup_id:
                enriched.append(reference)
                continue
            try:
                metadata = self.get_metadata(lookup_id, refresh=refresh)
            except ProviderError as exc:
                failed = dict(reference)
                failed["metadata_enriched"] = False
                failed["metadata_enrichment_error"] = {"code": exc.code, "message": exc.message}
                enriched.append(failed)
                continue
            enriched.append(_merge_reference_metadata(reference, metadata))
        return enriched

    def get_citers(
        self,
        paper_id: str,
        *,
        refresh: bool = False,
        limit: int = MAX_PAGE_SIZE,
        sort: str = "mostrecent",
    ) -> list[dict[str, Any]]:
        canonical_sort = _normalize_sort(sort)
        canonical_limit = _clamp_limit(limit)
        metadata = self.get_metadata(paper_id, refresh=refresh)
        recid = metadata.get("inspire_recid")
        if not recid:
            return []
        request = describe_inspire_citer_request(
            str(recid), sort=canonical_sort, limit=canonical_limit
        )

        params = {
            "q": f"refersto:recid:{recid}",
            "size": str(request.limit),
            "sort": request.sort,
            "fields": SUMMARY_FIELDS,
            "format": "json",
        }

        def fetch() -> Any:
            return self._request_json(
                f"{BASE_URL}/literature",
                params=params,
                error_code="inspire_citers_fetch_failed",
            )

        try:
            value = self.cache.fetch_json(
                "inspire-citers",
                request.request_key,
                fetch=fetch,
                refresh=refresh,
                payload_validator=_is_inspire_citers_raw_payload,
            )
        except RemoteCacheError as exc:
            if exc.code != "remote_cache_payload_contract_invalid":
                raise
            raise ProviderError(
                "inspire_response_invalid",
                "INSPIRE citer response does not satisfy the raw payload contract",
            ) from exc
        return [
            _normalize_record(hit)
            for hit in value["hits"]["hits"][: request.limit]
        ]

    def get_citer_count(self, paper_id: str, *, refresh: bool = False) -> int:
        return int(self.get_metadata(paper_id, refresh=refresh).get("citation_count") or 0)

    def get_raw_record(self, paper_id: str, *, refresh: bool = False) -> dict[str, Any]:
        normalized_id = normalize_paper_id(paper_id)
        if not normalized_id:
            raise ProviderError(
                "unsupported_paper_id",
                f"INSPIRE requires an arXiv ID, DOI, or INSPIRE recid: {paper_id}",
            )
        value = self.cache.fetch_json(
            "inspire-record",
            normalized_id,
            fetch=lambda: self._fetch_raw_record(
                normalized_id, requested_id=paper_id
            ),
            refresh=refresh,
        )
        if not isinstance(value, dict):
            raise ProviderError(
                "inspire_response_invalid", "INSPIRE record is not a JSON object"
            )
        return value

    def _fetch_raw_record(self, normalized_id: str, *, requested_id: str) -> dict[str, Any]:

        recid = inspire_recid(normalized_id)
        aid = arxiv_path_id(normalized_id)
        doi = doi_value(normalized_id)
        if recid:
            url = f"{BASE_URL}/literature/{recid}"
        elif aid:
            url = f"{BASE_URL}/arxiv/{aid}"
        elif doi:
            return self._get_raw_record_by_doi(doi, requested_id=normalized_id)
        else:
            raise ProviderError(
                "unsupported_paper_id",
                f"INSPIRE requires an arXiv ID, DOI, or INSPIRE recid: {requested_id}",
            )

        raw = self._request_json(
            url,
            error_code="inspire_fetch_failed",
            not_found_message=f"INSPIRE record not found for {requested_id}",
        )
        if not isinstance(raw, dict):
            raise ProviderError(
                "inspire_response_invalid", "INSPIRE record is not a JSON object"
            )
        return raw

    def _get_raw_record_by_doi(self, doi: str, *, requested_id: str) -> dict[str, Any]:
        data = self._request_json(
            f"{BASE_URL}/literature",
            params={"q": f"doi:{doi}", "size": "1", "format": "json"},
            error_code="inspire_fetch_failed",
        )
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            raise ProviderError("inspire_not_found", f"INSPIRE record not found for {requested_id}")
        return hits[0]

    def _request_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        error_code: str,
        not_found_message: str = "",
    ) -> Any:
        _require_inspire_url(url)
        response = self.client.get(url, params=params, timeout=self.timeout)
        if response.status_code == 404 and not_found_message:
            raise ProviderError("inspire_not_found", not_found_message)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                error_code, str(exc), status_code=exc.response.status_code
            ) from exc
        _require_inspire_url(str(response.url))
        content_length = response.headers.get("content-length")
        try:
            if content_length is not None and int(content_length) > MAX_JSON_BYTES:
                raise ProviderError(
                    "inspire_response_too_large",
                    f"INSPIRE response exceeds {MAX_JSON_BYTES} bytes",
                )
        except ValueError as exc:
            raise ProviderError(
                "remote_content_length_invalid",
                "INSPIRE response has an invalid Content-Length",
            ) from exc
        if len(response.content) > MAX_JSON_BYTES:
            raise ProviderError(
                "inspire_response_too_large",
                f"INSPIRE response exceeds {MAX_JSON_BYTES} bytes",
            )
        media_type = (
            response.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .casefold()
        )
        if media_type not in {"application/json", "application/vnd.api+json"}:
            raise ProviderError(
                "inspire_media_type_invalid",
                f"INSPIRE returned unsupported media type: {media_type or '<missing>'}",
            )
        try:
            return response.json()
        except (ValueError, UnicodeError) as exc:
            raise ProviderError(
                "inspire_response_invalid", "INSPIRE returned invalid JSON"
            ) from exc


def _require_inspire_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != INSPIRE_HOST:
        raise ProviderError(
            "remote_url_invalid",
            f"INSPIRE requests must use HTTPS on {INSPIRE_HOST}",
        )


def _normalize_record(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = payload.get("metadata", payload) or {}
    arxiv_id = _first_arxiv_id(metadata)
    recid = str(metadata.get("control_number") or payload.get("id") or "")
    paper_id = f"arXiv:{arxiv_id}" if arxiv_id else (f"inspire:{recid}" if recid else "")
    dois = _all_dois(metadata)
    doi = dois[0] if dois else ""
    return {
        "paper_id": paper_id,
        "title": _first_title(metadata),
        "abstract": _first_abstract(metadata),
        "authors": _authors(metadata),
        "arxiv_id": arxiv_id,
        "inspire_recid": recid,
        "doi": doi,
        "dois": dois,
        "identifiers": _identifiers(paper_id=paper_id, arxiv_id=arxiv_id, inspire_recid=recid, doi=doi),
        "year": _year(metadata),
        "published": str(metadata.get("earliest_date") or metadata.get("preprint_date") or ""),
        "citation_count": int(metadata.get("citation_count") or 0),
    }


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = MAX_PAGE_SIZE
    return max(1, min(value, MAX_PAGE_SIZE))


def _normalize_sort(sort: str) -> str:
    normalized = (sort or "mostrecent").strip().lower()
    if normalized not in {"mostrecent", "mostcited"}:
        raise ProviderError("unsupported_citer_sort", f"Unsupported INSPIRE citer sort: {sort}")
    return normalized


def _is_inspire_citers_raw_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    hits = value.get("hits")
    if not isinstance(hits, dict):
        return False
    records = hits.get("hits")
    return isinstance(records, list) and all(
        isinstance(record, dict) for record in records
    )


def _normalize_reference(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("reference") or item
    recid = _reference_recid(item)
    arxiv_id = _reference_arxiv_id(item)
    title = raw.get("title") or raw.get("titles") or ""
    paper_id = normalize_paper_id(arxiv_id) if arxiv_id else (f"inspire:{recid}" if recid else "")
    dois = _all_dois(raw)
    doi = dois[0] if dois else ""
    if not paper_id and not title and not raw:
        return {}
    out = {
        "paper_id": paper_id,
        "title": _clean_inspire_text(_string_or_first(title)),
        "raw_inspire_reference": item,
    }
    record_ref = (item.get("record") or {}).get("$ref")
    if record_ref:
        out["record_ref"] = str(record_ref)
    if publication_info := raw.get("publication_info"):
        out["publication_info"] = publication_info
    if abstract := _first_abstract(raw):
        out["abstract"] = abstract
    if authors := _authors(raw):
        out["authors"] = authors
    if arxiv_id:
        out["arxiv_id"] = arxiv_id
    if recid:
        out["inspire_recid"] = recid
    if doi:
        out["doi"] = doi
    if dois:
        out["dois"] = dois
    if identifiers := _identifiers(paper_id=paper_id, arxiv_id=arxiv_id, inspire_recid=recid, doi=doi):
        out["identifiers"] = identifiers
    if year := _year(raw):
        out["year"] = year
    published = str(raw.get("earliest_date") or raw.get("preprint_date") or "")
    if published:
        out["published"] = published
    if raw.get("citation_count") is not None:
        out["citation_count"] = int(raw.get("citation_count") or 0)
    return out


def _reference_lookup_id(reference: dict[str, Any]) -> str:
    if recid := reference.get("inspire_recid"):
        return f"inspire:{recid}"
    if paper_id := reference.get("paper_id"):
        return normalize_paper_id(str(paper_id))
    for doi in reference.get("dois") or []:
        if normalized := doi_value(str(doi)):
            return f"doi:{normalized}"
    if doi := reference.get("doi"):
        return normalize_paper_id(f"doi:{doi}")
    return ""


def _merge_reference_metadata(reference: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
    merged = dict(reference)
    merged["paper_id"] = metadata.get("paper_id") or merged.get("paper_id", "")
    merged["title"] = metadata.get("title") or merged.get("title", "")
    merged["abstract"] = metadata.get("abstract") or merged.get("abstract", "")
    merged["authors"] = metadata.get("authors") or merged.get("authors", [])
    for key in ("arxiv_id", "inspire_recid", "doi", "year", "published", "citation_count"):
        value = metadata.get(key)
        if value not in (None, "", []):
            merged[key] = value
        elif key not in merged:
            merged[key] = "" if key not in {"year", "citation_count"} else None
    merged_dois = _dedupe_dois(
        [
            *(metadata.get("dois") or []),
            metadata.get("doi") or "",
            *(reference.get("dois") or []),
            reference.get("doi") or "",
        ]
    )
    merged["dois"] = merged_dois
    merged["doi"] = merged_dois[0] if merged_dois else ""
    merged["identifiers"] = metadata.get("identifiers") or _identifiers(
        paper_id=str(merged.get("paper_id") or ""),
        arxiv_id=str(merged.get("arxiv_id") or ""),
        inspire_recid=str(merged.get("inspire_recid") or ""),
        doi=str(merged.get("doi") or ""),
    )
    merged["metadata_enriched"] = True
    merged.pop("metadata_enrichment_error", None)
    return merged


def _identifiers(*, paper_id: str, arxiv_id: str, inspire_recid: str, doi: str) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    if paper_id:
        identifiers["paper_id"] = paper_id
    if arxiv_id:
        identifiers["arxiv"] = f"arXiv:{arxiv_id}"
        identifiers["arxiv_id"] = arxiv_id
    if inspire_recid:
        identifiers["inspire"] = f"inspire:{inspire_recid}"
        identifiers["inspire_recid"] = inspire_recid
    if doi:
        identifiers["doi"] = doi
    return identifiers


def _reference_recid(item: dict[str, Any]) -> str:
    record = item.get("record") or {}
    return str(record.get("$ref", "").rstrip("/").split("/")[-1] or item.get("recid") or "")


def _reference_arxiv_id(item: dict[str, Any]) -> str:
    raw = item.get("reference") or item
    return _normalize_arxiv_value(raw.get("arxiv_eprint") or raw.get("arxiv_id") or raw.get("eprint") or "")


def _first_title(metadata: dict[str, Any]) -> str:
    titles = metadata.get("titles") or []
    if titles and isinstance(titles[0], dict):
        return _clean_inspire_text(titles[0].get("title"))
    return _clean_inspire_text(metadata.get("title"))


def _first_abstract(metadata: dict[str, Any]) -> str:
    abstracts = metadata.get("abstracts") or []
    if abstracts:
        first = abstracts[0]
        if isinstance(first, dict):
            return _clean_inspire_text(first.get("value") or first.get("summary"))
        return _clean_inspire_text(first)
    return _clean_inspire_text(metadata.get("abstract"))


def _first_arxiv_id(metadata: dict[str, Any]) -> str:
    for item in metadata.get("arxiv_eprints") or []:
        value = item.get("value") or item.get("eprint")
        if arxiv_id := _normalize_arxiv_value(value):
            return arxiv_id
    return ""


def _normalize_arxiv_value(value: Any) -> str:
    return arxiv_path_id(str(value or ""))


def _all_dois(metadata: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for item in metadata.get("dois") or []:
        if isinstance(item, dict):
            values.append(item.get("value") or "")
        else:
            values.append(item)
    if metadata.get("doi"):
        values.append(metadata["doi"])
    return _dedupe_dois(values)


def _dedupe_dois(values: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = doi_value(str(item or ""))
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _year(metadata: dict[str, Any]) -> int | None:
    for key in ("earliest_date", "preprint_date"):
        value = str(metadata.get(key) or "")
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    for item in metadata.get("publication_info") or []:
        if isinstance(item, dict) and item.get("year"):
            return int(item["year"])
    return None


def _authors(metadata: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for author in metadata.get("authors") or []:
        name = author.get("full_name") or author.get("name") or author.get("display_name")
        if name:
            names.append(str(name))
    return names


def _string_or_first(value: Any) -> str:
    if isinstance(value, list):
        if not value:
            return ""
        first = value[0]
        if isinstance(first, dict):
            return str(first.get("title") or first.get("value") or first.get("summary") or "").strip()
        return str(first).strip()
    return str(value or "").strip()


def _clean_inspire_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = MATHML_RE.sub(lambda match: _mathml_to_text(match.group(0)), text)
    text = HTML_TAG_RE.sub("", text)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:)\]\}])", r"\1", text)
    text = re.sub(r"([(\[\{])\s+", r"\1", text)
    return text


def _mathml_to_text(markup: str) -> str:
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return _clean_inspire_text(HTML_TAG_RE.sub("", markup))
    return _normalize_math_text(_math_node_text(root))


def _math_node_text(node: ET.Element) -> str:
    tag = _local_name(node.tag)
    children = list(node)
    if tag in {"math", "mrow", "mstyle", "mpadded", "mphantom"}:
        return _math_children_text(node)
    if tag in {"mi", "mn", "mo", "mtext"}:
        return _math_token_text((node.text or "") + _math_child_elements_text(node))
    if tag == "msub" and len(children) >= 2:
        return f"{_math_node_text(children[0])}_{_math_node_text(children[1])}"
    if tag == "msup" and len(children) >= 2:
        return f"{_math_node_text(children[0])}^{_math_node_text(children[1])}"
    if tag == "msubsup" and len(children) >= 3:
        return f"{_math_node_text(children[0])}_{_math_node_text(children[1])}^{_math_node_text(children[2])}"
    if tag == "mfrac" and len(children) >= 2:
        return f"({_math_node_text(children[0])})/({_math_node_text(children[1])})"
    if tag == "msqrt" and children:
        return f"sqrt({_math_children_text(node)})"
    if tag == "mroot" and len(children) >= 2:
        return f"root({_math_node_text(children[0])},{_math_node_text(children[1])})"
    if tag in {"semantics", "menclose"} and children:
        return _math_node_text(children[0])
    if tag in {"annotation", "annotation-xml"}:
        return ""
    return _math_children_text(node) or _math_token_text(node.text or "")


def _math_children_text(node: ET.Element) -> str:
    parts = []
    if node.text and node.text.strip():
        parts.append(node.text)
    parts.append(_math_child_elements_text(node))
    return "".join(parts)


def _math_child_elements_text(node: ET.Element) -> str:
    parts = []
    for child in list(node):
        if _local_name(child.tag) not in {"annotation", "annotation-xml"}:
            parts.append(_math_node_text(child))
        if child.tail and child.tail.strip():
            parts.append(child.tail)
    return "".join(parts)


def _math_token_text(text: str) -> str:
    return html.unescape(text).replace("\u2062", "").replace("\xa0", " ").strip()


def _normalize_math_text(text: str) -> str:
    text = WHITESPACE_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([,.;:)\]\}])", r"\1", text)
    text = re.sub(r"([(\[\{])\s+", r"\1", text)
    return text


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
