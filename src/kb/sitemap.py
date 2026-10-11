"""Sitemap discovery that fails closed: a full URL set with lastmod, or an explicit failure."""

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from kb import http
from kb.urls import normalize_url

MAX_DEPTH = 5


@dataclass(frozen=True)
class SitemapEntry:
    key: str
    loc: str
    lastmod: datetime | None = None


@dataclass
class SitemapResult:
    entries: list[SitemapEntry] = field(default_factory=list)
    error: str | None = None
    failed_url: str | None = None
    sitemaps_read: int = 0
    skipped: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class _Failure(Exception):
    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(reason)


def parse_lastmod(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return None


def parse_document(data: bytes, url: str) -> tuple[str, list[tuple[str, str | None]]]:
    """Return ("index" | "urlset", [(loc, lastmod)]) or raise _Failure."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as e:
            raise _Failure(url, f"invalid gzip: {e}") from None
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise _Failure(url, f"not valid sitemap XML ({e})") from None

    kind = _local(root.tag)
    if kind == "sitemapindex":
        item = "sitemap"
    elif kind == "urlset":
        item = "url"
    else:
        raise _Failure(url, f"unexpected root element <{kind}>")

    items: list[tuple[str, str | None]] = []
    for el in root:
        if _local(el.tag) != item:
            continue
        loc = _child_text(el, "loc")
        if loc:
            items.append((loc, _child_text(el, "lastmod")))
    return ("index" if item == "sitemap" else "urlset"), items


async def discover(
    client: httpx.AsyncClient,
    sitemap_url: str,
    policy: http.HttpPolicy,
    max_depth: int = MAX_DEPTH,
    sleep: http.Sleep | None = None,
) -> SitemapResult:
    result = SitemapResult()
    found: dict[str, SitemapEntry] = {}
    visited: set[str] = set()
    kwargs = {"sleep": sleep} if sleep else {}

    async def walk(url: str, depth: int) -> None:
        try:
            key = normalize_url(url)
        except ValueError as e:
            raise _Failure(url, f"invalid sitemap URL ({e})") from None
        if key in visited:
            return
        if depth > max_depth:
            raise _Failure(url, f"sitemap nesting deeper than {max_depth} levels")
        visited.add(key)

        try:
            resp = await http.get(client, url, policy, **kwargs)
        except http.FetchError as e:
            raise _Failure(url, str(e)) from None
        if resp.status != 200:
            raise _Failure(url, f"HTTP {resp.status}")

        kind, items = parse_document(resp.content, url)
        result.sitemaps_read += 1
        if kind == "index":
            for loc, _ in items:
                await walk(loc, depth + 1)
            return

        for loc, lastmod in items:
            if not loc.lower().startswith(("http://", "https://")):
                continue
            try:
                key = normalize_url(loc)
            except ValueError:
                result.skipped += 1  # one malformed page URL must not fail the whole site
                continue
            entry = SitemapEntry(key=key, loc=loc, lastmod=parse_lastmod(lastmod))
            prev = found.get(entry.key)
            if prev is None or (entry.lastmod and (prev.lastmod is None or entry.lastmod > prev.lastmod)):
                found[entry.key] = entry

    try:
        await walk(sitemap_url, 0)
    except _Failure as f:
        return SitemapResult(error=f.reason, failed_url=f.url, sitemaps_read=result.sitemaps_read)
    except ValueError as e:  # anything else malformed in the document fails closed too
        return SitemapResult(error=f"malformed sitemap ({e})", failed_url=sitemap_url, sitemaps_read=result.sitemaps_read)

    result.entries = list(found.values())
    return result
