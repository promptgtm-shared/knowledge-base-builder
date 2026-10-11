"""Regression tests for issues found re-reviewing the hardening fixes."""

import json
from pathlib import Path

from typer.testing import CliRunner

from kb import config, locks
from kb.cli import app
from kb.fetch import FetchOutcome, apply_outcome, content_hash
from kb.sitemap import SitemapEntry
from kb.state import PageStatus, SiteState, load_state


def cli(root: Path, *args: str):
    return CliRunner().invoke(app, ["--root", str(root), *args])


def test_state_with_list_contexts_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "pages": {}, "qmd_contexts": ["qmd://kb-x/"]}), encoding="utf-8")
    assert load_state(path).qmd_contexts == {}


def test_existing_sites_json_with_old_names_still_loads(tmp_path: Path) -> None:
    (tmp_path / "sites.json").write_text(json.dumps({
        "sites": {"Old_Site": {"id": "Old_Site", "sitemap_url": "https://x.example/s.xml", "taxonomy": {"Product_Features": "P"}}}
    }), encoding="utf-8")
    out = cli(tmp_path, "status")
    assert out.exit_code == 0 and "Old_Site" in out.output


def test_bad_site_id_message_is_readable(tmp_path: Path) -> None:
    out = cli(tmp_path, "site", "add", "https://example.com/sitemap.xml", "--id", "Bad_Id")
    assert out.exit_code == 2 and "lowercase letters" in out.output


def test_healed_page_is_not_flipped_back_by_same_content_refetch(tmp_path: Path) -> None:
    from test_finalize import NOW, Env

    env = Env(tmp_path / "ws")
    md = "# Lost\n\n" + "Same content as before. " * 10
    h = content_hash(md)
    env.page("lost", topic="guides", chash=h, content=h)
    (env.paths.raw / "lost.md").write_text(md, encoding="utf-8")
    env.run()  # wiki file missing -> requeued
    page = env.state.pages["https://example.com/lost"]
    assert page.status == PageStatus.PENDING and page.compiled_hash is None

    apply_outcome(env.state, SitemapEntry(key=page.key, loc=page.loc), FetchOutcome(page.key, "content", markdown=md),
                  env.paths.raw, NOW)
    assert env.state.pages[page.key].status == PageStatus.PENDING


def test_alias_leftover_in_fetch_error_is_not_restored(tmp_path: Path) -> None:
    from test_finalize import Env

    env = Env(tmp_path / "ws")
    archived = env.paths.archive / "news" / "dup.md"
    archived.parent.mkdir(parents=True)
    archived.write_text("dup", encoding="utf-8")
    env.page("dup", status=PageStatus.FETCH_ERROR, topic="news", chash="c1", content="c1", alias_of="https://example.com/main")
    result = env.run()
    assert result.restored == 0 and archived.exists() and not (env.paths.wiki / "news" / "dup.md").exists()


def test_unlock_only_removes_stale_or_corrupt_locks(tmp_path: Path) -> None:
    lock_file = config.WorkspacePaths(tmp_path).lock_file
    locks.acquire(lock_file)
    assert cli(tmp_path, "sync", "unlock").exit_code == 2
    lock_file.write_text("{broken", encoding="utf-8")
    out = cli(tmp_path, "sync", "unlock")
    assert out.exit_code == 0 and not lock_file.exists()
    assert "No lock held" in cli(tmp_path, "sync", "unlock").output


def test_batches_with_lost_lock_is_a_clean_error(tmp_path: Path) -> None:
    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    run_dir = config.WorkspacePaths(tmp_path).site("example-com").staging / "abcdef123456"
    run_dir.mkdir(parents=True)
    (run_dir / "queue.json").write_text("[]", encoding="utf-8")
    out = cli(tmp_path, "sync", "batches", "example-com", "--run-id", "abcdef123456")
    assert out.exit_code == 2 and "does not hold the workspace lock" in out.output


def test_long_host_gives_valid_site_id(tmp_path: Path) -> None:
    host = "a" * 80 + ".example"
    site = config.add_site(tmp_path, f"https://{host}/sitemap.xml")
    assert len(site.id) <= 63


def test_state_unused_guard() -> None:
    assert SiteState().qmd_contexts == {}
