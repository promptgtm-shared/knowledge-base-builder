import asyncio
import gzip
from datetime import UTC, datetime

import httpx
import respx

from kb.http import HttpPolicy, create_client
from kb.sitemap import discover, parse_lastmod

POLICY = HttpPolicy(max_retries=2)
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


async def _no_sleep(_: float) -> None:
    return None


def urlset(*items: tuple[str, str | None]) -> str:
    body = "".join(f"<url><loc>{u}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>" for u, m in items)
    return f'<?xml version="1.0"?><urlset {NS}>{body}</urlset>'


def index(*locs: str) -> str:
    body = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f'<?xml version="1.0"?><sitemapindex {NS}>{body}</sitemapindex>'


def run(url: str = "https://example.com/sitemap.xml"):
    async def go():
        async with create_client(POLICY) as client:
            return await discover(client, url, POLICY, sleep=_no_sleep)

    return asyncio.run(go())


@respx.mock
def test_flat_sitemap_with_optional_lastmod() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=urlset(("https://example.com/a", "2026-01-02"), ("https://example.com/b/", None)))
    )
    result = run()
    assert result.ok
    by_key = {e.key: e for e in result.entries}
    assert by_key["https://example.com/a"].lastmod == datetime(2026, 1, 2, tzinfo=UTC)
    assert by_key["https://example.com/b"].lastmod is None


@respx.mock
def test_three_level_nesting_deduped() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text=index("https://example.com/s1.xml")))
    respx.get("https://example.com/s1.xml").mock(
        return_value=httpx.Response(200, text=index("https://example.com/posts.xml", "https://example.com/pages.xml"))
    )
    respx.get("https://example.com/posts.xml").mock(
        return_value=httpx.Response(200, text=urlset(("https://example.com/p1", "2026-01-01"), ("https://example.com/shared", "2026-01-01")))
    )
    respx.get("https://example.com/pages.xml").mock(
        return_value=httpx.Response(200, text=urlset(("https://example.com/shared/", "2026-03-01")))
    )
    result = run()
    assert result.ok
    assert sorted(e.key for e in result.entries) == ["https://example.com/p1", "https://example.com/shared"]
    shared = next(e for e in result.entries if e.key.endswith("shared"))
    assert shared.lastmod == datetime(2026, 3, 1, tzinfo=UTC)
    assert result.sitemaps_read == 4


@respx.mock
def test_gzipped_sub_sitemap() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text=index("https://example.com/s.xml.gz")))
    respx.get("https://example.com/s.xml.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(urlset(("https://example.com/z", None)).encode()))
    )
    result = run()
    assert result.ok and [e.key for e in result.entries] == ["https://example.com/z"]


@respx.mock
def test_index_cycle_terminates() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=index("https://example.com/b.xml", "https://example.com/u.xml"))
    )
    respx.get("https://example.com/b.xml").mock(return_value=httpx.Response(200, text=index("https://example.com/sitemap.xml")))
    respx.get("https://example.com/u.xml").mock(return_value=httpx.Response(200, text=urlset(("https://example.com/x", None))))
    result = run()
    assert result.ok and len(result.entries) == 1


@respx.mock
def test_html_body_is_failure_not_empty() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text="<html><body>Oops</body></html>"))
    result = run()
    assert not result.ok
    assert result.entries == []
    assert "unexpected root" in result.error or "XML" in result.error


@respx.mock
def test_one_failed_sub_sitemap_fails_everything() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=index("https://example.com/1.xml", "https://example.com/2.xml", "https://example.com/3.xml"))
    )
    respx.get("https://example.com/1.xml").mock(return_value=httpx.Response(200, text=urlset(("https://example.com/a", None))))
    respx.get("https://example.com/2.xml").mock(return_value=httpx.Response(500))
    respx.get("https://example.com/3.xml").mock(return_value=httpx.Response(200, text=urlset(("https://example.com/c", None))))
    result = run()
    assert not result.ok
    assert result.failed_url == "https://example.com/2.xml"
    assert result.entries == []


@respx.mock
def test_root_404_is_failure() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(404))
    result = run()
    assert not result.ok and "404" in result.error


@respx.mock
def test_genuinely_empty_urlset_is_success() -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, text=urlset()))
    result = run()
    assert result.ok and result.entries == []


@respx.mock
def test_missing_namespace_still_parsed() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text="<urlset><url><loc>https://example.com/n</loc></url></urlset>")
    )
    assert [e.key for e in run().entries] == ["https://example.com/n"]


def test_parse_lastmod_variants() -> None:
    assert parse_lastmod("2026-05-01T10:00:00Z") == datetime(2026, 5, 1, 10, tzinfo=UTC)
    assert parse_lastmod("2026-05-01T10:00:00+02:00").utcoffset().total_seconds() == 7200
    assert parse_lastmod("garbage") is None
    assert parse_lastmod(None) is None
