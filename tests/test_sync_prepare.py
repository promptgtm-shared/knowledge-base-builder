import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from kb import config, locks, sync
from kb.cli import app
from kb.http import HttpPolicy
from kb.state import PageStatus, load_state, save_state

SITEMAP = "https://example.com/sitemap.xml"
POLICY = HttpPolicy(max_retries=1, concurrency=2)
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


async def _no_sleep(_: float) -> None:
    return None


def body(title: str) -> str:
    return f"<html><body><article><h1>{title}</h1><p>{'Substantial content sentence. ' * 8}</p></article></body></html>"


def urlset(pages: dict[str, str | None]) -> str:
    items = "".join(
        f"<url><loc>https://example.com/{p}</loc>{f'<lastmod>{m}</lastmod>' if m else ''}</url>" for p, m in pages.items()
    )
    return f"<urlset {NS}>{items}</urlset>"


def mock_site(pages: dict[str, str | None], bodies: dict[str, str] | None = None) -> None:
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=urlset(pages)))
    for p in pages:
        respx.get(f"https://example.com/{p}").mock(return_value=httpx.Response(200, text=(bodies or {}).get(p, body(p))))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    config.add_site(tmp_path, SITEMAP)
    return tmp_path


def run_prepare(root: Path, run_id=None, **kw):
    return sync.prepare(root, "example-com", run_id, policy=POLICY, sleep=_no_sleep, **kw)


def mark_all_compiled(root: Path) -> None:
    paths = config.WorkspacePaths(root).site("example-com")
    state = load_state(paths.state_file)
    for p in state.pages.values():
        if p.status == PageStatus.PENDING:
            p.status, p.topic, p.compiled_hash, p.queued_at = PageStatus.COMPILED, "general", p.content_hash, None
    state.progress = None
    save_state(paths.state_file, state)


@respx.mock
def test_first_sync_fetches_everything_and_writes_queue(root: Path) -> None:
    mock_site({"a": "2026-01-01", "b": None, "c": "2026-01-02"})
    result = run_prepare(root)
    assert result.status == "ready"
    assert result.diff["new"] == 3 and result.queue_size == 3 and result.deferred == 0
    queue = json.loads(Path(result.queue_file).read_text(encoding="utf-8"))
    assert sorted(q["slug"] for q in queue) == ["a", "b", "c"]
    assert all(Path(q["raw_path"]).exists() and q["raw_hash"] for q in queue)
    assert config.load_workspace(root).sites["example-com"].first_sync_complete
    assert locks.read_lock(config.WorkspacePaths(root).lock_file).run_id == result.run_id


@respx.mock
def test_unchanged_site_second_run_fetches_lastmod_less_only_and_queues_nothing(root: Path) -> None:
    mock_site({"a": "2026-01-01", "b": None})
    first = run_prepare(root)
    sync.abort(root, first.run_id)
    mark_all_compiled(root)

    respx.reset()
    mock_site({"a": "2026-01-01", "b": None})
    second = run_prepare(root)
    assert second.status == "ready"
    assert second.diff["unchanged"] == 1 and second.diff["no_lastmod"] == 1
    assert second.fetched == {"same_content": 1}
    assert second.queue_size == 0
    assert respx.get("https://example.com/a").call_count == 0


@respx.mock
def test_time_budget_returns_incomplete_and_resumes(root: Path, monkeypatch) -> None:
    pages = {f"p{i}": "2026-01-01" for i in range(15)}
    mock_site(pages)
    # deadline, checkpoint start and first loop check see t=0; time has run out after the first chunk
    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr(sync, "_clock", lambda: next(ticks, 1000.0))
    first = run_prepare(root, time_budget_s=100)
    assert first.status == "incomplete" and first.remaining_to_fetch == 5

    monkeypatch.setattr(sync, "_clock", lambda: 0.0)
    calls_before = sum(r.call_count for r in respx.routes)
    second = run_prepare(root, run_id=first.run_id)
    assert second.status == "ready" and second.queue_size == 15
    fetched_now = sum(r.call_count for r in respx.routes) - calls_before
    assert fetched_now == 5  # only the remaining chunk; no sitemap re-read


@respx.mock
def test_sitemap_failure_changes_nothing(root: Path) -> None:
    mock_site({"a": "2026-01-01"})
    first = run_prepare(root)
    sync.abort(root, first.run_id)
    mark_all_compiled(root)
    respx.reset()
    respx.get(SITEMAP).mock(return_value=httpx.Response(500))
    result = run_prepare(root)
    assert result.status == "failed" and "Nothing was changed" in result.message
    state = load_state(config.WorkspacePaths(root).site("example-com").state_file)
    assert state.pages["https://example.com/a"].status == PageStatus.COMPILED
    assert state.progress is None


@respx.mock
def test_mass_removal_held_then_approved(root: Path) -> None:
    pages = {f"p{i}": "2026-01-01" for i in range(12)}
    mock_site(pages)
    run = run_prepare(root)
    sync.abort(root, run.run_id)
    mark_all_compiled(root)

    respx.reset()
    mock_site({f"p{i}": "2026-01-01" for i in range(8)})
    held = run_prepare(root)
    assert len(held.archive_held) == 4 and held.archive_pending == 0
    sync.abort(root, held.run_id)

    respx.reset()
    mock_site({f"p{i}": "2026-01-01" for i in range(8)})
    approved = run_prepare(root, approve_archive=True)
    assert approved.archive_held == [] and approved.archive_pending == 4


@respx.mock
def test_budget_caps_queue(root: Path) -> None:
    mock_site({f"p{i}": "2026-01-01" for i in range(6)})
    result = run_prepare(root, budget=4)
    assert result.queue_size == 4 and result.deferred == 2


@respx.mock
def test_second_concurrent_run_is_skipped_and_abort_releases(root: Path) -> None:
    mock_site({"a": "2026-01-01"})
    first = run_prepare(root)
    with pytest.raises(locks.LockHeld):
        run_prepare(root)
    staging = config.WorkspacePaths(root).site("example-com").staging / first.run_id
    assert staging.exists()
    assert sync.abort(root, first.run_id)
    assert not staging.exists()
    assert locks.read_lock(config.WorkspacePaths(root).lock_file) is None


def test_cli_prepare_skipped_when_locked(root: Path) -> None:
    locks.acquire(config.WorkspacePaths(root).lock_file, label="other run")
    result = CliRunner().invoke(app, ["--root", str(root), "sync", "prepare", "example-com"])
    assert result.exit_code == 3
    assert json.loads(result.output)["status"] == "skipped"


def test_cli_prepare_rejects_local_site(root: Path) -> None:
    config.ensure_local_site(root)
    result = CliRunner().invoke(app, ["--root", str(root), "sync", "prepare", "local"])
    assert result.exit_code == 2 and "kb ingest" in result.output


def test_remaining_budget(root: Path) -> None:
    assert sync.remaining_budget(root, 0) == 50
    assert sync.remaining_budget(root, 70) == 0
