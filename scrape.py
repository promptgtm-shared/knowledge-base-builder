"""Sitemap scraper that converts all pages to clean markdown (no links)."""

from __future__ import annotations

import argparse
import asyncio
import random
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse

import warnings

import html2text
import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
from pydantic import BaseModel
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

console = Console()


class ScraperConfig(BaseModel):
    sitemap_url: str
    output_dir: Path = Path("raw")
    concurrency: int = 10
    min_delay: float = 0.5
    max_delay: float = 2.0
    max_retries: int = 3
    timeout: float = 30.0


def get_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


def create_client(config: ScraperConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        follow_redirects=True,
        timeout=httpx.Timeout(config.timeout),
        limits=httpx.Limits(
            max_connections=config.concurrency + 5,
            max_keepalive_connections=config.concurrency,
        ),
    )


# --- Sitemap parsing ---


def parse_sitemap_index(xml_data: bytes) -> list[str]:
    root = ET.fromstring(xml_data)
    return [
        loc.text.strip()
        for loc in root.findall(f"{{{SITEMAP_NS}}}sitemap/{{{SITEMAP_NS}}}loc")
        if loc.text
    ]


def parse_sitemap_urls(xml_data: bytes) -> list[str]:
    root = ET.fromstring(xml_data)
    return [
        loc.text.strip()
        for loc in root.findall(f"{{{SITEMAP_NS}}}url/{{{SITEMAP_NS}}}loc")
        if loc.text
    ]


async def fetch_xml(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, headers=get_headers())
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError as e:
        console.print(f"[red]Failed to fetch sitemap: {url} — {e}[/red]")
        return None


async def discover_all_urls(
    client: httpx.AsyncClient, sitemap_url: str
) -> list[str]:
    console.print(f"[cyan]Fetching sitemap index: {sitemap_url}[/cyan]")
    xml_data = await fetch_xml(client, sitemap_url)
    if not xml_data:
        return []

    # Try as sitemap index first
    sub_sitemaps = parse_sitemap_index(xml_data)
    if sub_sitemaps:
        console.print(f"[cyan]Found {len(sub_sitemaps)} sub-sitemaps[/cyan]")
        all_urls: list[str] = []
        for sub_url in sub_sitemaps:
            console.print(f"  [dim]Parsing: {sub_url}[/dim]")
            sub_xml = await fetch_xml(client, sub_url)
            if sub_xml:
                # Check if sub-sitemap is itself an index (recursive)
                nested = parse_sitemap_index(sub_xml)
                if nested:
                    for nested_url in nested:
                        nested_xml = await fetch_xml(client, nested_url)
                        if nested_xml:
                            all_urls.extend(parse_sitemap_urls(nested_xml))
                else:
                    all_urls.extend(parse_sitemap_urls(sub_xml))
            await asyncio.sleep(random.uniform(0.2, 0.5))

        urls = list(dict.fromkeys(all_urls))  # deduplicate, preserve order
        console.print(f"[green]Discovered {len(urls)} unique pages[/green]")
        return urls

    # Flat sitemap
    urls = parse_sitemap_urls(xml_data)
    urls = list(dict.fromkeys(urls))
    console.print(f"[green]Discovered {len(urls)} unique pages[/green]")
    return urls


# --- Content extraction ---

CONTENT_SELECTORS = [
    ".entry-content",
    "article",
    "main",
    "[role='main']",
    ".post-content",
    ".page-content",
    ".content",
]


def extract_content(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    # Remove script, style, nav, footer, header, sidebar elements
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
        tag.decompose()

    # Try content selectors in priority order
    for selector in CONTENT_SELECTORS:
        element = soup.select_one(selector)
        if element and len(element.get_text(strip=True)) > 100:
            return str(element)

    # Fallback to body
    body = soup.find("body")
    return str(body) if body else html


def html_to_markdown(html: str) -> str:
    content_html = extract_content(html)

    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = False
    h.body_width = 0
    h.unicode_snob = True
    h.skip_internal_links = True
    h.single_line_break = False
    h.protect_links = False
    h.wrap_links = False
    h.wrap_list_items = False

    md = h.handle(content_html)

    # Post-processing: clean up
    md = re.sub(r"\n{3,}", "\n\n", md)  # collapse excessive blank lines
    md = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)  # strip any residual markdown links
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)  # strip any residual images
    md = md.strip()

    return md


# --- File operations ---


def url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    if not path:
        return "index.md"

    # Flatten hierarchy: / → --
    filename = path.replace("/", "--")
    # Remove URL-encoded chars and sanitize
    filename = re.sub(r"[^a-zA-Z0-9_\-]", "-", filename)
    filename = re.sub(r"-{2,}", "-", filename)
    filename = filename.strip("-")

    return f"{filename}.md"


def save_markdown(content: str, output_dir: Path, filename: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return filepath


# --- Page fetching with retry ---


async def fetch_page(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    config: ScraperConfig,
) -> tuple[str, str | None]:
    async with semaphore:
        await asyncio.sleep(random.uniform(config.min_delay, config.max_delay))

        for attempt in range(config.max_retries):
            try:
                resp = await client.get(url, headers=get_headers())

                if resp.status_code == 404:
                    return (url, None)

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                    await asyncio.sleep(retry_after)
                    continue

                resp.raise_for_status()
                return (url, resp.text)

            except httpx.TimeoutException:
                if attempt < config.max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
            except httpx.ConnectError:
                if attempt < config.max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < config.max_retries - 1:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                return (url, None)

        return (url, None)


# --- Main pipeline ---


async def scrape_sitemap(config: ScraperConfig) -> None:
    domain = urlparse(config.sitemap_url).netloc
    output_path = config.output_dir / domain

    async with create_client(config) as client:
        # Phase 1: Discover all URLs
        urls = await discover_all_urls(client, config.sitemap_url)
        if not urls:
            console.print("[red]No URLs found in sitemap.[/red]")
            return

        # Phase 2: Fetch and convert
        semaphore = asyncio.Semaphore(config.concurrency)
        success = 0
        failed = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[status]}[/dim]"),
            console=console,
        ) as progress:
            task_id = progress.add_task(
                f"Scraping {domain}", total=len(urls), status=""
            )

            tasks = [fetch_page(client, semaphore, url, config) for url in urls]

            for coro in asyncio.as_completed(tasks):
                url, html = await coro
                if html:
                    try:
                        md = html_to_markdown(html)
                        if md and len(md) > 50:
                            filename = url_to_filename(url)
                            save_markdown(md, output_path, filename)
                            success += 1
                        else:
                            failed += 1
                    except Exception as e:
                        console.print(f"[yellow]Convert error: {url} — {e}[/yellow]")
                        failed += 1
                else:
                    failed += 1

                progress.update(task_id, advance=1, status=f"[green]{success}[/green] ok, [red]{failed}[/red] skip")

    console.print()
    console.print(f"[bold green]Done![/bold green] {success} pages saved to {output_path}")
    if failed:
        console.print(f"[yellow]{failed} pages skipped (404, empty, or error)[/yellow]")


# --- CLI ---


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape all pages from a sitemap and convert to clean markdown."
    )
    parser.add_argument("sitemap_url", help="URL of the sitemap XML")
    parser.add_argument("-o", "--output-dir", default="raw", help="Output directory (default: raw)")
    parser.add_argument("-c", "--concurrency", type=int, default=10, help="Max concurrent requests (default: 10)")
    parser.add_argument("--min-delay", type=float, default=0.5, help="Min delay between requests in seconds (default: 0.5)")
    parser.add_argument("--max-delay", type=float, default=2.0, help="Max delay between requests in seconds (default: 2.0)")
    parser.add_argument("--max-retries", type=int, default=3, help="Max retries per page (default: 3)")

    args = parser.parse_args()

    config = ScraperConfig(
        sitemap_url=args.sitemap_url,
        output_dir=Path(args.output_dir),
        concurrency=args.concurrency,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        max_retries=args.max_retries,
    )

    asyncio.run(scrape_sitemap(config))


if __name__ == "__main__":
    main()
