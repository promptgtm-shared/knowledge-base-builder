import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from kb.fetch import FetchOutcome, apply_outcome, content_hash, fetch_one
from kb.http import HttpPolicy, create_client
from kb.sitemap import SitemapEntry
from kb.state import Page, PageStatus, SiteState

POLICY = HttpPolicy(max_retries=2, concurrency=2)
NOW = datetime(2026, 3, 1, tzinfo=UTC)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)
BODY = "<html><body><article><h1>Title</h1><p>" + "Plenty of real content here. " * 10 + "</p></article></body></html>"
BODY2 = BODY.replace("Title", "New Title")


async def _no_sleep(_: float) -> None:
    return None


def url(path: str) -> str:
    return f"https://example.com/{path}"


def entry(path: str, lastmod=T1) -> SitemapEntry:
    return SitemapEntry(key=url(path), loc=url(path), lastmod=lastmod)


def fetch(e: SitemapEntry, page: Page | None = None, sitemap_keys: set[str] | None = None) -> FetchOutcome:
    async def go():
        async with create_client(POLICY) as client:
            return await fetch_one(client, asyncio.Semaphore(2), e, page, POLICY, sitemap_keys or {e.key}, _no_sleep)

    return asyncio.run(go())


def compiled_page(path: str, md_hash: str, **kw) -> Page:
    return Page(
        key=url(path), loc=url(path), slug=path, status=PageStatus.COMPILED, lastmod=T0,
        content_hash=md_hash, compiled_hash=md_hash, topic="general", **kw,
    )


def body_hash(html: str) -> str:
    from kb.convert import html_to_markdown

    return content_hash(html_to_markdown(html))


# --- fetch_one ---


@respx.mock
def test_content_outcome_records_validators() -> None:
    respx.get(url("a")).mock(return_value=httpx.Response(200, html=BODY, headers={"ETag": '"v1"'}))
    out = fetch(entry("a"))
    assert out.kind == "content" and out.etag == '"v1"' and "Title" in out.markdown


@respx.mock
def test_conditional_get_304() -> None:
    route = respx.get(url("a")).mock(return_value=httpx.Response(304))
    page = compiled_page("a", "h", etag='"v1"')
    assert fetch(entry("a"), page).kind == "not_modified"
    assert route.calls[0].request.headers["If-None-Match"] == '"v1"'


@respx.mock
def test_redirect_to_other_sitemap_url_is_alias() -> None:
    respx.get(url("old")).mock(return_value=httpx.Response(301, headers={"Location": url("new")}))
    respx.get(url("new")).mock(return_value=httpx.Response(200, html=BODY))
    out = fetch(entry("old"), sitemap_keys={url("old"), url("new")})
    assert out.kind == "alias" and out.final_url == url("new")


@respx.mock
def test_off_site_redirect_is_error() -> None:
    respx.get(url("x")).mock(return_value=httpx.Response(302, headers={"Location": "https://login.other.com/"}))
    respx.get("https://login.other.com/").mock(return_value=httpx.Response(200, html=BODY))
    out = fetch(entry("x"))
    assert out.kind == "error" and "off-site" in out.error


@respx.mock
def test_404_and_empty_and_5xx_are_errors() -> None:
    respx.get(url("gone")).mock(return_value=httpx.Response(404))
    respx.get(url("empty")).mock(return_value=httpx.Response(200, html="<html><body><p>hi</p></body></html>"))
    respx.get(url("down")).mock(return_value=httpx.Response(503))
    assert fetch(entry("gone")).error == "HTTP 404"
    assert "empty" in fetch(entry("empty")).error
    assert fetch(entry("down")).kind == "error"


@respx.mock
def test_429_retry_after_then_success() -> None:
    respx.get(url("a")).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), httpx.Response(200, html=BODY)])
    assert fetch(entry("a")).kind == "content"


# --- apply_outcome ---


def test_new_page_written_and_queued(tmp_path: Path) -> None:
    state = SiteState()
    label = apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown="# A\n\n" + "x" * 80), tmp_path, NOW)
    page = state.pages[url("a")]
    assert label == "changed"
    assert page.status == PageStatus.PENDING and page.queued_at == NOW and page.lastmod == T1
    raw = (tmp_path / "a.md").read_text(encoding="utf-8")
    assert raw.startswith("---\nsource: https://example.com/a\n") and "# A" in raw


def test_lastmod_bump_with_same_content_stays_compiled(tmp_path: Path) -> None:
    md = "# A\n\n" + "y" * 80
    state = SiteState(pages={url("a"): compiled_page("a", content_hash(md))})
    label = apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown=md), tmp_path, NOW)
    page = state.pages[url("a")]
    assert label == "same_content"
    assert page.status == PageStatus.COMPILED and page.lastmod == T1
    assert not (tmp_path / "a.md").exists()


def test_whitespace_only_change_is_same_hash() -> None:
    assert content_hash("# A\n\ntext") == content_hash("# A  \r\n\n\n\ntext\n")


def test_changed_content_requeues_and_counts_streak(tmp_path: Path) -> None:
    state = SiteState(pages={url("a"): compiled_page("a", "old")})
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown="# B\n\n" + "z" * 80), tmp_path, NOW)
    page = state.pages[url("a")]
    assert page.status == PageStatus.PENDING and page.change_streak == 1 and page.compiled_hash == "old"


def test_fetch_error_keeps_article_and_recovers(tmp_path: Path) -> None:
    md = "# A\n\n" + "y" * 80
    state = SiteState(pages={url("a"): compiled_page("a", content_hash(md))})
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "error", error="HTTP 503"), tmp_path, NOW)
    page = state.pages[url("a")]
    assert page.status == PageStatus.FETCH_ERROR and page.topic == "general" and page.lastmod == T0
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown=md), tmp_path, NOW)
    assert page.status == PageStatus.COMPILED and page.last_error is None


def test_error_on_pending_page_keeps_it_queued(tmp_path: Path) -> None:
    state = SiteState(pages={url("a"): Page(key=url("a"), loc=url("a"), slug="a", status=PageStatus.PENDING, content_hash="h", queued_at=T0)})
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "error", error="timeout"), tmp_path, NOW)
    assert state.pages[url("a")].status == PageStatus.PENDING


def test_reappeared_same_hash_stays_archived_for_restore(tmp_path: Path) -> None:
    md = "# A\n\n" + "y" * 80
    page = compiled_page("a", content_hash(md))
    page.status = PageStatus.ARCHIVED
    state = SiteState(pages={url("a"): page})
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown=md), tmp_path, NOW)
    assert page.status == PageStatus.ARCHIVED
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "content", markdown=md + " more"), tmp_path, NOW)
    assert page.status == PageStatus.PENDING


def test_alias_outcome(tmp_path: Path) -> None:
    state = SiteState(pages={url("old"): compiled_page("old", "h")})
    apply_outcome(state, entry("old"), FetchOutcome(url("old"), "alias", final_url=url("new")), tmp_path, NOW)
    assert state.pages[url("old")].status == PageStatus.ALIAS and state.pages[url("old")].alias_of == url("new")


def test_new_page_error_is_recorded_for_retry(tmp_path: Path) -> None:
    state = SiteState()
    apply_outcome(state, entry("a"), FetchOutcome(url("a"), "error", error="HTTP 500"), tmp_path, NOW)
    assert state.pages[url("a")].status == PageStatus.FETCH_ERROR
