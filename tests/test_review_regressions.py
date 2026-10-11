"""Regression tests for code-review findings on the sync pipeline."""

import json
from pathlib import Path

import httpx
import respx
from typer.testing import CliRunner

from kb import config, locks, staging, sync
from kb.cli import app
from kb.state import Page, PageStatus, load_state, save_state

SITEMAP = "https://example.com/sitemap.xml"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def serve(slugs: list[str]) -> None:
    items = "".join(f"<url><loc>https://example.com/{s}</loc><lastmod>2026-01-01</lastmod></url>" for s in slugs)
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=f"<urlset {NS}>{items}</urlset>"))
    body = "<html><body><article><h1>T</h1><p>" + "Real page content sentence. " * 10 + "</p></article></body></html>"
    for s in slugs:
        respx.get(f"https://example.com/{s}").mock(return_value=httpx.Response(200, html=body.replace("T", s)))


def seed_compiled(root: Path, slugs: list[str]) -> None:
    paths = config.WorkspacePaths(root).site("example-com")
    state = load_state(paths.state_file)
    for s in slugs:
        key = f"https://example.com/{s}"
        state.pages[key] = Page(key=key, loc=key, slug=s, status=PageStatus.COMPILED, topic="general",
                                compiled_hash="h", content_hash="h")
    save_state(paths.state_file, state)


def cli(root: Path, *args: str):
    return CliRunner().invoke(app, ["--root", str(root), "sync", "prepare", "example-com", "--time-budget", "60", *args])


@respx.mock
def test_cli_unattended_applies_remaining_cap(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    serve(["a", "b", "c"])
    out = json.loads(cli(tmp_path, "--unattended", "--already-queued", "49").output)
    assert out["status"] == "ready" and out["queue_size"] == 1 and out["deferred"] == 2


@respx.mock
def test_cli_unattended_ignores_approve_archive(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    seed_compiled(tmp_path, [f"p{i}" for i in range(12)])
    paths = config.WorkspacePaths(tmp_path).site("example-com")
    state = load_state(paths.state_file)
    state.held_archive = [f"https://example.com/p{i}" for i in range(8, 12)]
    save_state(paths.state_file, state)

    serve([f"p{i}" for i in range(8)])
    out = json.loads(cli(tmp_path, "--unattended", "--approve-archive").output)
    assert len(out["archive_held"]) == 4 and out["archive_pending"] == 0


@respx.mock
def test_pages_leaving_sitemap_are_not_queued(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    paths = config.WorkspacePaths(tmp_path).site("example-com")
    state = load_state(paths.state_file)
    key = "https://example.com/gone"
    state.pages[key] = Page(key=key, loc=key, slug="gone", status=PageStatus.PENDING, content_hash="x")
    save_state(paths.state_file, state)

    serve(["kept"])
    result = sync.prepare(tmp_path, "example-com", sleep=_no_sleep)
    queued = [q.slug for q in staging.load_queue(Path(result.queue_file).parent)]
    assert queued == ["kept"]
    assert result.archive_pending == 1
    locks.release(config.WorkspacePaths(tmp_path).lock_file, result.run_id)


async def _no_sleep(_: float) -> None:
    return None


def test_recompiling_archived_page_keeps_created(tmp_path: Path) -> None:
    from test_finalize import ARTICLE, NOW, Env

    env = Env(tmp_path / "ws")
    archived = env.paths.archive / "news" / "back.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("# Back\n\n**Source:** https://example.com/back\n**Created:** 2025-05-05\n\nold", encoding="utf-8")
    env.page("back", status=PageStatus.PENDING, topic="news", chash="old", content="c2")
    env.stage("back", "news", body=ARTICLE.format(title="Back again"))
    env.run()
    text = (env.paths.wiki / "news" / "back.md").read_text(encoding="utf-8")
    assert "**Created:** 2025-05-05" in text and f"**Updated:** {NOW.date().isoformat()}" in text
