import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kb import config, locks
from kb.finalize import FinalizeError, finalize, render_article
from kb.fsutil import atomic_write_json
from kb.state import FetchProgress, Page, PageStatus, SiteState, load_state, save_state
from kb.sync import QUEUE_FILE, QueueItem

SITE = "example-com"
NOW = datetime(2026, 4, 1, 12, tzinfo=UTC)
ARTICLE = "# {title}\n\n## Summary\n\n" + "A solid summary sentence for the article. " * 6 + "\n\n## Key Takeaways\n\n- One\n- Two\n"


def url(slug: str) -> str:
    return f"https://example.com/{slug}"


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        config.add_site(root, "https://example.com/sitemap.xml")
        ws = config.load_workspace(root)
        ws.sites[SITE].taxonomy = {"guides": "Guides", "news": "News", "general": "General"}
        config.save_workspace(root, ws)
        self.paths = config.WorkspacePaths(root).site(SITE)
        self.run_id = locks.acquire(config.WorkspacePaths(root).lock_file)
        self.state = SiteState()
        self.queue: list[QueueItem] = []
        self.manifest: list[dict] = []
        self.removed: list[str] = []
        self.sitemap: set[str] = set()

    @property
    def run_dir(self) -> Path:
        return self.paths.staging / self.run_id

    def page(self, slug: str, status=PageStatus.COMPILED, topic=None, chash=None, content="c1", **kw) -> Page:
        p = Page(key=url(slug), loc=url(slug), slug=slug, status=status, topic=topic, compiled_hash=chash, content_hash=content, **kw)
        self.state.pages[p.key] = p
        if status != PageStatus.ARCHIVED or kw.get("in_sitemap"):
            self.sitemap.add(p.key)
        return p

    def wiki_article(self, rel: str, title: str = "Old", created: str = "2026-01-01", source: str | None = None) -> Path:
        path = self.paths.wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        slug = Path(rel).stem
        path.write_text(
            f"# {title}\n\n**Source:** {source or url(slug)}\n**Created:** {created}\n**Topics:** X\n\n" + "old body " * 40,
            encoding="utf-8",
        )
        return path

    def stage(self, slug: str, topic: str, title: str = "New Title", body: str | None = None) -> None:
        page = self.state.pages[url(slug)]
        self.queue.append(QueueItem(slug=slug, key=page.key, loc=page.loc, raw_path="", raw_hash=page.content_hash))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / f"{slug}.md").write_text(body if body is not None else ARTICLE.format(title=title), encoding="utf-8")
        self.manifest.append({"slug": slug, "topic": topic})

    def run(self, run_id: str | None = None):
        self.state.progress = FetchProgress(
            run_id=self.run_id, sitemap_keys=sorted(self.sitemap), removed=self.removed, fetch_done=True,
            diff_counts={"new": 1}, fetch_counts={"changed": 1},
        )
        save_state(self.paths.state_file, self.state)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.run_dir / QUEUE_FILE, [q.model_dump() for q in self.queue])
        atomic_write_json(self.run_dir / "manifest-1.json", self.manifest)
        result = finalize(self.root, SITE, run_id or self.run_id, now=NOW)
        self.state = load_state(self.paths.state_file)
        return result


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_staged_articles_published_into_topics(env: Env) -> None:
    for slug, topic in (("a", "guides"), ("b", "news"), ("c", "guides")):
        env.page(slug, status=PageStatus.PENDING)
        env.stage(slug, topic, title=f"Title {slug}")
    result = env.run()
    assert result.compiled == 3 and result.status == "ok" and result.qmd_dirty
    text = (env.paths.wiki / "guides" / "a.md").read_text(encoding="utf-8")
    assert text.startswith("# Title a\n\n**Source:** https://example.com/a\n**Created:** 2026-04-01\n**Topics:** Guides\n")
    assert "**Updated:**" not in text
    assert env.state.pages[url("a")].status == PageStatus.COMPILED
    assert env.state.pages[url("a")].compiled_hash == "c1"
    assert "[Title a](a.md)" in (env.paths.wiki / "guides" / "_index.md").read_text(encoding="utf-8")
    master = (env.paths.wiki / "_master-index.md").read_text(encoding="utf-8")
    assert "| [Guides](guides/_index.md) | 2 |" in master
    assert env.state.progress is None
    assert Path(result.report).exists()


def test_reclassified_page_moves_and_old_topic_cleaned(env: Env) -> None:
    env.wiki_article("news/a.md", created="2025-12-01")
    env.page("a", status=PageStatus.PENDING, topic="news", chash="old", content="c2")
    env.stage("a", "guides")
    env.run()
    assert not (env.paths.wiki / "news").exists()
    text = (env.paths.wiki / "guides" / "a.md").read_text(encoding="utf-8")
    assert "**Created:** 2025-12-01" in text and "**Updated:** 2026-04-01" in text
    assert "news" not in (env.paths.wiki / "_master-index.md").read_text(encoding="utf-8")


def test_missing_staged_file_stays_queued(env: Env) -> None:
    env.page("a", status=PageStatus.PENDING)
    env.page("b", status=PageStatus.PENDING)
    env.stage("a", "guides")
    env.queue.append(QueueItem(slug="b", key=url("b"), loc=url("b"), raw_path="", raw_hash="c1"))
    result = env.run()
    assert result.compiled == 1 and result.status == "partial"
    assert result.failed == ["b: no staged article"]
    assert env.state.pages[url("b")].status == PageStatus.PENDING


def test_stale_output_from_crashed_run_is_ignored(env: Env) -> None:
    env.page("a", status=PageStatus.PENDING, content="c-new")
    old_run = env.paths.staging / "oldrun"
    old_run.mkdir(parents=True)
    (old_run / "a.md").write_text(ARTICLE.format(title="Stale"), encoding="utf-8")
    atomic_write_json(old_run / "manifest-1.json", [{"slug": "a", "topic": "guides"}])
    # Current run deferred page a by the cap: it is not in this run's queue
    result = env.run()
    assert result.compiled == 0
    assert env.state.pages[url("a")].status == PageStatus.PENDING
    assert not (env.paths.wiki / "guides" / "a.md").exists()


def test_queue_entry_with_outdated_hash_is_skipped(env: Env) -> None:
    env.page("a", status=PageStatus.PENDING, content="c1")
    env.stage("a", "guides")
    env.state.pages[url("a")].content_hash = "c2"
    assert env.run().compiled == 0


def test_unknown_topic_and_too_short_article(env: Env) -> None:
    env.page("a", status=PageStatus.PENDING)
    env.stage("a", "made-up-topic")
    env.page("b", status=PageStatus.PENDING)
    env.stage("b", "guides", body="# Tiny\n\nshort")
    result = env.run()
    assert (env.paths.wiki / "general" / "a.md").exists()
    assert result.failed and result.failed[0].startswith("b:")


def test_removed_page_archived_and_unarticled_dropped(env: Env) -> None:
    env.wiki_article("news/gone.md")
    env.page("gone", topic="news", chash="c1")
    env.sitemap.discard(url("gone"))
    env.page("never-compiled", status=PageStatus.PENDING)
    env.sitemap.discard(url("never-compiled"))
    (env.paths.raw / "never-compiled.md").write_text("raw", encoding="utf-8")
    env.removed = [url("gone"), url("never-compiled")]
    result = env.run()
    assert result.archived == 1 and result.dropped == 1
    assert (env.paths.archive / "news" / "gone.md").exists()
    assert not (env.paths.wiki / "news" / "gone.md").exists()
    assert env.state.pages[url("gone")].status == PageStatus.ARCHIVED
    assert url("never-compiled") not in env.state.pages
    assert not (env.paths.raw / "never-compiled.md").exists()


def test_held_archive_moves_nothing(env: Env) -> None:
    env.wiki_article("news/gone.md")
    env.page("gone", topic="news", chash="c1")
    env.state.held_archive = [url("gone")]
    result = env.run()
    assert result.archived == 0 and result.archive_held == 1
    assert (env.paths.wiki / "news" / "gone.md").exists()
    assert "--approve-archive" in Path(result.report).read_text(encoding="utf-8")


def test_reappeared_same_hash_restored_without_compile(env: Env) -> None:
    archived = env.paths.archive / "news" / "back.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("# Back\n\n**Source:** https://example.com/back\n\n" + "body " * 50, encoding="utf-8")
    env.page("back", status=PageStatus.ARCHIVED, topic="news", chash="c1", content="c1", in_sitemap=True)
    result = env.run()
    assert result.restored == 1
    assert (env.paths.wiki / "news" / "back.md").exists() and not archived.exists()
    assert env.state.pages[url("back")].status == PageStatus.COMPILED


def test_recompile_of_archived_page_removes_archived_copy(env: Env) -> None:
    archived = env.paths.archive / "news" / "back.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("old", encoding="utf-8")
    env.page("back", status=PageStatus.PENDING, topic="news", chash="old", content="c2")
    env.stage("back", "news")
    env.run()
    assert not archived.exists() and (env.paths.wiki / "news" / "back.md").exists()


def test_alias_article_archived(env: Env) -> None:
    env.wiki_article("news/old.md")
    env.page("old", status=PageStatus.ALIAS, topic="news", chash="c1", alias_of=url("new"))
    result = env.run()
    assert result.archived == 1 and (env.paths.archive / "news" / "old.md").exists()


def test_reconcile_removes_tracked_duplicate_but_keeps_user_notes(env: Env) -> None:
    env.wiki_article("guides/a.md")
    dup = env.wiki_article("news/a.md")
    env.page("a", topic="guides", chash="c1")
    note = env.paths.wiki / "guides" / "my-notes.md"
    note.write_text("# My notes\n\nPersonal.", encoding="utf-8")
    legacy = env.wiki_article("guides/legacy.md", source="raw/md/legacy.md")
    result = env.run()
    assert not dup.exists() and result.reconciled == ["news/a.md"]
    assert note.exists() and legacy.exists()
    assert sorted(result.untracked) == ["guides/legacy.md", "guides/my-notes.md"]


def test_no_changes_leaves_qmd_clean(env: Env) -> None:
    env.wiki_article("guides/a.md")
    env.page("a", topic="guides", chash="c1")
    result = env.run()
    assert not result.qmd_dirty and result.compiled == 0


def test_requires_lock_and_completed_prepare(env: Env) -> None:
    with pytest.raises(locks.LockLost):
        env.run(run_id="not-the-owner")
    save_state(env.paths.state_file, SiteState())
    with pytest.raises(FinalizeError, match="prepare"):
        finalize(env.root, SITE, env.run_id, now=NOW)


def test_render_article_strips_agent_header_lines() -> None:
    body = "Intro junk\n# Title\n**Source:** wrong\n**Topics:** Wrong\n\nText here.\n\n\n\nMore."
    out = render_article(body, "https://example.com/x", "Guides", "2026-01-01", "2026-02-01")
    assert out == (
        "# Title\n\n**Source:** https://example.com/x\n**Created:** 2026-01-01\n**Updated:** 2026-02-01\n"
        "**Topics:** Guides\n\nText here.\n\nMore.\n"
    )
    with pytest.raises(FinalizeError):
        render_article("no heading", "s", "G", "d", None)


def test_manifest_ignores_garbage(env: Env) -> None:
    env.page("a", status=PageStatus.PENDING)
    env.stage("a", "guides")
    env.run_dir.mkdir(parents=True, exist_ok=True)
    (env.run_dir / "manifest-0.json").write_text("{not json", encoding="utf-8")
    assert env.run().compiled == 1
    assert json.loads((env.run_dir / QUEUE_FILE).read_text(encoding="utf-8"))[0]["slug"] == "a"
