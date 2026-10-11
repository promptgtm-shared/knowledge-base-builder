"""Fetch pages, decide whether content really changed, and apply the result to state."""

from __future__ import annotations

import asyncio
import hashlib
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx

from kb import http
from kb.convert import html_to_markdown
from kb.fsutil import atomic_write_text
from kb.sitemap import SitemapEntry
from kb.state import Page, PageStatus, SiteState
from kb.urls import assign_slug, normalize_url

MIN_CONTENT_CHARS = 50
MAX_PAGE_BYTES = 5_000_000
HTML_TYPES = ("html", "xml")


def normalize_markdown(md: str) -> str:
    lines = [line.rstrip() for line in md.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def content_hash(md: str) -> str:
    return hashlib.sha256(normalize_markdown(md).encode("utf-8")).hexdigest()


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


@dataclass
class FetchOutcome:
    key: str
    kind: Literal["content", "not_modified", "alias", "error", "deferred"]
    markdown: str | None = None
    final_url: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    error: str | None = None


async def fetch_one(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    entry: SitemapEntry,
    page: Page | None,
    policy: http.HttpPolicy,
    sitemap_keys: set[str],
    sleep: http.Sleep | None = None,
    expired: Callable[[], bool] | None = None,
) -> FetchOutcome:
    """Fetch one page. Returns kind "deferred" without a request once `expired()` is true."""
    headers: dict[str, str] = {}
    if page and page.content_hash:
        if page.etag:
            headers["If-None-Match"] = page.etag
        if page.last_modified:
            headers["If-Modified-Since"] = page.last_modified

    async with sem:
        if expired and expired():
            return FetchOutcome(entry.key, "deferred")
        if sleep is None:
            await asyncio.sleep(random.uniform(policy.min_delay, policy.max_delay))
        try:
            resp = await http.get(client, entry.loc, policy, extra_headers=headers, **({"sleep": sleep} if sleep else {}))
        except http.FetchError as e:
            return FetchOutcome(entry.key, "error", error=str(e))

    validators = {"etag": resp.headers.get("etag"), "last_modified": resp.headers.get("last-modified")}
    if resp.status == 304:
        return FetchOutcome(entry.key, "not_modified", **validators)
    if resp.status != 200:
        return FetchOutcome(entry.key, "error", error=f"HTTP {resp.status}")
    content_type = resp.headers.get("content-type", "").lower()
    if content_type and not any(t in content_type for t in HTML_TYPES):
        return FetchOutcome(entry.key, "error", final_url=resp.url, error=f"not an HTML page ({content_type.split(';')[0]})")
    if len(resp.content) > MAX_PAGE_BYTES:
        return FetchOutcome(entry.key, "error", final_url=resp.url, error=f"page larger than {MAX_PAGE_BYTES // 1_000_000} MB")

    final_key = normalize_url(resp.url)
    if final_key != entry.key:
        if _host(final_key) != _host(entry.key):
            return FetchOutcome(entry.key, "error", final_url=resp.url, error=f"redirected off-site to {resp.url}")
        if final_key in sitemap_keys:
            return FetchOutcome(entry.key, "alias", final_url=final_key)

    try:
        # Parsing is CPU-bound; keep the event loop free for the other in-flight requests
        md = await asyncio.to_thread(html_to_markdown, resp.text)
    except Exception as e:  # noqa: BLE001 - one bad page must not stop the run
        return FetchOutcome(entry.key, "error", final_url=resp.url, error=f"convert failed: {e}")
    if len(md) < MIN_CONTENT_CHARS:
        return FetchOutcome(entry.key, "error", final_url=resp.url, error="empty page after extraction")
    return FetchOutcome(entry.key, "content", markdown=md, final_url=resp.url, **validators)


def raw_document(md: str, source: str, fetched: datetime) -> str:
    return f"---\nsource: {source}\nfetched: {fetched.isoformat()}\n---\n\n{normalize_markdown(md)}\n"


def apply_outcome(
    state: SiteState,
    entry: SitemapEntry,
    outcome: FetchOutcome,
    raw_dir: Path,
    now: datetime,
    taken: set[str] | None = None,
) -> str:
    """Update the page record for one fetch; returns a short label for run counters.

    Pass a shared `taken` slug set when applying many outcomes to avoid rebuilding it per page.
    """
    page = state.pages.get(entry.key)
    if page is None:
        taken = taken if taken is not None else state.taken_slugs()
        page = Page(key=entry.key, loc=entry.loc, slug=assign_slug(entry.key, taken), first_seen=now)
        taken.add(page.slug)
        state.pages[entry.key] = page
        page.status = PageStatus.FETCH_ERROR  # replaced below on success

    page.loc = entry.loc
    if outcome.kind == "error":
        page.last_error = outcome.error
        if page.status == PageStatus.COMPILED:
            page.status = PageStatus.FETCH_ERROR
        return "error"

    page.last_error = None
    page.last_fetched = now
    page.lastmod = entry.lastmod
    if outcome.etag is not None or outcome.last_modified is not None:
        page.etag, page.last_modified = outcome.etag, outcome.last_modified

    if outcome.kind == "alias":
        page.alias_of = outcome.final_url
        page.final_url = outcome.final_url
        page.status = PageStatus.ARCHIVED if page.status == PageStatus.ARCHIVED else PageStatus.ALIAS
        return "alias"

    if page.status == PageStatus.ALIAS:
        page.status = PageStatus.ARCHIVED if page.has_article else PageStatus.PENDING
    page.alias_of = None
    if outcome.final_url:
        page.final_url = outcome.final_url

    if outcome.kind == "content":
        new_hash = content_hash(outcome.markdown or "")
        if new_hash != page.content_hash:
            if page.content_hash is not None:
                page.change_streak += 1
            page.content_hash = new_hash
            atomic_write_text(raw_dir / f"{page.slug}.md", raw_document(outcome.markdown or "", entry.loc, now))
            label = "changed"
        else:
            page.change_streak = 0
            label = "same_content"
    else:
        page.change_streak = 0
        label = "not_modified"

    settle_status(page, now)
    return label


def settle_status(page: Page, now: datetime) -> None:
    """After a successful fetch: compiled if the article matches the content, else queue it."""
    if page.status == PageStatus.ARCHIVED:
        if page.content_hash != page.compiled_hash:
            page.status = PageStatus.PENDING
            page.queued_at = page.queued_at or now
        return  # same content: finalize restores the archived article without an LLM call
    if page.has_article and page.content_hash == page.compiled_hash:
        page.status = PageStatus.COMPILED
        page.queued_at = None
    elif page.content_hash is not None:
        # A page already waiting keeps its original queue time so it isn't starved
        if page.status != PageStatus.PENDING or page.queued_at is None:
            page.queued_at = now
        page.status = PageStatus.PENDING
    else:
        page.status = PageStatus.FETCH_ERROR
