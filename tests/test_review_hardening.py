"""Regression tests for the second round of code-review fixes (robustness and input validation)."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from kb import config, locks, sync
from kb.cli import app
from kb.fetch import FetchOutcome, apply_outcome, fetch_one
from kb.http import HttpPolicy, create_client
from kb.qmd import Qmd, QmdError, refresh, subprocess_runner
from kb.sitemap import SitemapEntry, discover
from kb.state import PageStatus, load_state, save_state

SITEMAP = "https://example.com/sitemap.xml"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
POLICY = HttpPolicy(max_retries=1, concurrency=2)


async def _no_sleep(_: float) -> None:
    return None


def runner_invoke(root: Path, *args: str):
    return CliRunner().invoke(app, ["--root", str(root), *args])


# --- #1 malformed sitemap entries ---


@respx.mock
def test_malformed_loc_is_skipped_not_fatal() -> None:
    body = f"<urlset {NS}><url><loc>https://example.com:99999/bad</loc></url><url><loc>https://example.com/ok</loc></url></urlset>"
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=body))

    async def go():
        async with create_client(POLICY) as client:
            return await discover(client, SITEMAP, POLICY, sleep=_no_sleep)

    result = asyncio.run(go())
    assert result.ok and [e.key for e in result.entries] == ["https://example.com/ok"] and result.skipped == 1


@respx.mock
def test_malformed_child_sitemap_fails_closed() -> None:
    body = f"<sitemapindex {NS}><sitemap><loc>https://example.com:99999/s.xml</loc></sitemap></sitemapindex>"
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=body))

    async def go():
        async with create_client(POLICY) as client:
            return await discover(client, SITEMAP, POLICY, sleep=_no_sleep)

    result = asyncio.run(go())
    assert not result.ok and "invalid sitemap URL" in result.error


# --- #8 lock released when a step crashes ---


def test_prepare_crash_releases_the_lock_it_took(tmp_path: Path, monkeypatch) -> None:
    config.add_site(tmp_path, SITEMAP)

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(sync, "load_state", boom)
    with pytest.raises(RuntimeError):
        sync.prepare(tmp_path, "example-com", sleep=_no_sleep)
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_prepare_crash_keeps_a_joined_runs_lock(tmp_path: Path, monkeypatch) -> None:
    config.add_site(tmp_path, SITEMAP)
    run_id = locks.acquire(config.WorkspacePaths(tmp_path).lock_file)
    monkeypatch.setattr(sync, "load_state", lambda *_: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        sync.prepare(tmp_path, "example-com", run_id, sleep=_no_sleep)
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file).run_id == run_id


def test_refresh_releases_lock_on_unexpected_error(tmp_path: Path) -> None:
    config.add_site(tmp_path, "https://alpha.example/sitemap.xml", site_id="alpha")
    paths = config.WorkspacePaths(tmp_path).site("alpha")
    state = load_state(paths.state_file)
    state.qmd_dirty = True
    save_state(paths.state_file, state)
    run_id = locks.acquire(config.WorkspacePaths(tmp_path).lock_file)

    def broken(args, timeout):
        raise RuntimeError("unexpected")

    result = refresh(tmp_path, run_id, Qmd(broken))
    assert result.status == "failed" and locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


# --- #4 run ids are validated before anything is deleted ---


def test_abort_rejects_path_like_run_ids(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    victim = tmp_path / "keep"
    victim.mkdir()
    for bad in (str(victim), "../../keep", "abc"):
        with pytest.raises(config.ConfigError):
            sync.abort(tmp_path, bad)
    assert victim.exists()
    out = runner_invoke(tmp_path, "sync", "abort", "--run-id", str(victim))
    assert out.exit_code == 2 and "Invalid run id" in out.output


# --- #7 names that become paths and qmd arguments ---


def test_site_ids_and_topic_folders_are_validated(tmp_path: Path) -> None:
    for bad in ("..", "C:/x", "a&calc", "Upper", "-lead"):
        with pytest.raises(config.ConfigError):
            config.add_site(tmp_path, f"https://{abs(hash(bad))}.example/sitemap.xml", site_id=bad)
    config.add_site(tmp_path, SITEMAP)
    tax = tmp_path / "tax.json"
    tax.write_text(json.dumps({"../evil": "Evil"}), encoding="utf-8")
    out = runner_invoke(tmp_path, "site", "update", "example-com", "--taxonomy-file", str(tax))
    assert out.exit_code == 2 and "Topic folder" in out.output


def test_qmd_runner_refuses_cmd_metacharacters(monkeypatch) -> None:
    monkeypatch.setattr("kb.qmd.shutil.which", lambda _: "qmd")
    with pytest.raises(QmdError, match="cmd.exe"):
        subprocess_runner()(["collection", "add", "C:/Me & You/wiki"], 5)


# --- #5 / #14 articles that should be in the wiki come back ---


def test_alias_that_stops_redirecting_is_restored(tmp_path: Path) -> None:
    from test_finalize import NOW, Env

    from kb.fetch import content_hash

    env = Env(tmp_path / "ws")
    md = "# Old\n\n" + "Unchanged body text. " * 10
    h = content_hash(md)
    archived = env.paths.archive / "news" / "old.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("# Old\n\n**Source:** https://example.com/old\n\nbody", encoding="utf-8")
    page = env.page("old", status=PageStatus.ALIAS, topic="news", chash=h, content=h, alias_of="https://example.com/new")

    # Next prepare: the URL serves its own, unchanged content again
    label = apply_outcome(env.state, SitemapEntry(key=page.key, loc=page.loc), FetchOutcome(page.key, "content", markdown=md),
                          env.paths.raw, NOW)
    assert label == "same_content" and env.state.pages[page.key].status == PageStatus.ARCHIVED
    result = env.run()
    assert result.restored == 1 and (env.paths.wiki / "news" / "old.md").exists()
    assert env.state.pages[page.key].status == PageStatus.COMPILED


def test_missing_archive_copy_requeues_instead_of_sticking(tmp_path: Path) -> None:
    from test_finalize import Env

    env = Env(tmp_path / "ws")
    env.page("back", status=PageStatus.ARCHIVED, topic="news", chash="c1", content="c1", in_sitemap=True)
    result = env.run()
    page = env.state.pages["https://example.com/back"]
    assert result.requeued == 1 and page.status == PageStatus.PENDING
    assert page.content_hash is None  # no raw file: next prepare refetches


def test_compiled_page_with_deleted_article_is_healed(tmp_path: Path) -> None:
    from test_finalize import Env

    env = Env(tmp_path / "ws")
    env.page("lost", topic="guides", chash="c1", content="c1")
    (env.paths.raw / "lost.md").write_text("raw", encoding="utf-8")
    result = env.run()
    assert result.requeued == 1 and env.state.pages["https://example.com/lost"].status == PageStatus.PENDING


def test_missing_manifest_keeps_previous_topic(tmp_path: Path) -> None:
    from test_finalize import Env

    env = Env(tmp_path / "ws")
    env.wiki_article("guides/a.md")
    env.page("a", status=PageStatus.PENDING, topic="guides", chash="old", content="c2")
    env.stage("a", "guides")
    env.manifest.clear()  # agent died before writing its manifest
    env.run()
    assert (env.paths.wiki / "guides" / "a.md").exists() and not (env.paths.wiki / "general").exists()


# --- #12 non-HTML and oversized responses ---


@respx.mock
def test_non_html_and_oversized_pages_are_errors() -> None:
    respx.get("https://example.com/file.pdf").mock(return_value=httpx.Response(200, content=b"%PDF", headers={"Content-Type": "application/pdf"}))
    respx.get("https://example.com/huge").mock(return_value=httpx.Response(200, html="<p>" + "x" * 5_100_000 + "</p>"))

    def get(path: str) -> FetchOutcome:
        entry = SitemapEntry(key=f"https://example.com/{path}", loc=f"https://example.com/{path}")

        async def go():
            async with create_client(POLICY) as client:
                return await fetch_one(client, asyncio.Semaphore(1), entry, None, POLICY, {entry.key}, _no_sleep)

        return asyncio.run(go())

    assert "not an HTML page (application/pdf)" in get("file.pdf").error
    assert "larger than" in get("huge").error


# --- #9 deadline checked before every request ---


def test_expired_deadline_defers_without_requesting() -> None:
    entry = SitemapEntry(key="https://example.com/a", loc="https://example.com/a")

    async def go():
        async with create_client(POLICY) as client:
            return await fetch_one(client, asyncio.Semaphore(1), entry, None, POLICY, {entry.key}, _no_sleep, lambda: True)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(entry.loc).mock(return_value=httpx.Response(200, html="<p>x</p>"))
        assert asyncio.run(go()).kind == "deferred" and route.call_count == 0


# --- #18 qmd contexts follow label changes ---


def test_changed_label_and_recreated_collection_resend_contexts(tmp_path: Path) -> None:
    from test_qmd import FakeQmd, setup

    run_id = setup(tmp_path, {"alpha": True})
    refresh(tmp_path, run_id, Qmd(FakeQmd()))

    ws = config.load_workspace(tmp_path)
    ws.sites["alpha"].taxonomy["general"] = "Everything else"
    config.save_workspace(tmp_path, ws)
    paths = config.WorkspacePaths(tmp_path).site("alpha")
    state = load_state(paths.state_file)
    state.qmd_dirty = True
    save_state(paths.state_file, state)
    fake = FakeQmd(existing={"kb-alpha"})
    refresh(tmp_path, locks.acquire(config.WorkspacePaths(tmp_path).lock_file), Qmd(fake))
    assert [c[2:] for c in fake.calls if c[0] == "context"] == [["qmd://kb-alpha/general", "Everything else articles."]]

    state = load_state(paths.state_file)
    state.qmd_dirty = True
    save_state(paths.state_file, state)
    recreated = FakeQmd()  # collection gone: every context is sent again
    refresh(tmp_path, locks.acquire(config.WorkspacePaths(tmp_path).lock_file), Qmd(recreated))
    assert len([c for c in recreated.calls if c[0] == "context"]) == len(ws.sites["alpha"].taxonomy) + 1


# --- agent-native CLI gaps ---


def test_workspace_cap_and_site_add_json(tmp_path: Path) -> None:
    assert json.loads(runner_invoke(tmp_path, "workspace", "cap").output) == {"unattended_cap": 50}
    assert json.loads(runner_invoke(tmp_path, "workspace", "cap", "20").output) == {"unattended_cap": 20}
    assert runner_invoke(tmp_path, "workspace", "cap", "0").exit_code == 2
    added = json.loads(runner_invoke(tmp_path, "site", "add", SITEMAP).output)
    assert added["id"] == "example-com" and added["wiki"].endswith("wiki")


@respx.mock
def test_budget_is_a_total_across_sites(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    items = "".join(f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(4))
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=f"<urlset {NS}>{items}</urlset>"))
    for i in range(4):
        respx.get(f"https://example.com/p{i}").mock(return_value=httpx.Response(200, html="<article><h1>T</h1><p>" + "words " * 30 + "</p></article>"))
    out = json.loads(runner_invoke(tmp_path, "sync", "prepare", "example-com", "--budget", "5", "--already-queued", "3").output)
    assert out["queue_size"] == 2 and out["deferred"] == 2
