"""Sync orchestration steps called by the CLI: prepare (resumable) and abort."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from kb import config, http, locks
from kb.diff import approval_matches, build_queue, classify
from kb.fetch import apply_outcome, fetch_one
from kb.fsutil import atomic_write_json
from kb.sitemap import SitemapEntry, discover
from kb.state import FetchProgress, PendingFetch, SiteState, load_state, save_state

COST_PER_PAGE_USD = 0.004
DEFAULT_TIME_BUDGET_S = 480.0
QUEUE_FILE = "queue.json"


def _clock() -> float:
    return time.monotonic()


class QueueItem(BaseModel):
    slug: str
    key: str
    loc: str
    raw_path: str
    raw_hash: str
    current_topic: str | None = None
    is_update: bool = False


class PrepareResult(BaseModel):
    status: Literal["incomplete", "ready", "failed"]
    site: str
    run_id: str
    message: str = ""
    diff: dict[str, int] = Field(default_factory=dict)
    fetched: dict[str, int] = Field(default_factory=dict)
    remaining_to_fetch: int = 0
    queue_size: int = 0
    deferred: int = 0
    churning: int = 0
    estimated_cost_usd: float = 0.0
    archive_pending: int = 0
    archive_held: list[str] = Field(default_factory=list)
    queue_file: str | None = None


def run_staging(paths: config.SitePaths, run_id: str) -> Path:
    return paths.staging / run_id


def _start_run(
    state: SiteState,
    run_id: str,
    entries: list[SitemapEntry],
    full: bool,
    approve_archive: bool,
) -> None:
    diff = classify(state, entries, full=full)
    approved = approve_archive and approval_matches(state, diff.removed)
    if diff.archive_held and not approved:
        state.held_archive = diff.removed
        removed: list[str] = []
    else:
        state.held_archive = []
        removed = diff.removed

    state.progress = FetchProgress(
        run_id=run_id,
        remaining=[PendingFetch(key=e.key, loc=e.loc, lastmod=e.lastmod) for e in diff.to_fetch()],
        sitemap_keys=[e.key for e in entries],
        removed=removed,
        diff_counts=diff.counts(),
    )


async def _fetch_chunks(
    state: SiteState,
    state_path: Path,
    raw_dir: Path,
    policy: http.HttpPolicy,
    deadline: float,
    sleep: http.Sleep | None,
    now_fn,
) -> None:
    progress = state.progress
    assert progress is not None
    sitemap_keys = set(progress.sitemap_keys)
    chunk_size = max(policy.concurrency * 5, 10)
    counts = Counter(progress.fetch_counts)

    async with http.create_client(policy) as client:
        sem = asyncio.Semaphore(policy.concurrency)
        while progress.remaining and _clock() < deadline:
            chunk = progress.remaining[:chunk_size]
            entries = [SitemapEntry(key=p.key, loc=p.loc, lastmod=p.lastmod) for p in chunk]
            outcomes = await asyncio.gather(
                *(fetch_one(client, sem, e, state.pages.get(e.key), policy, sitemap_keys, sleep) for e in entries)
            )
            now = now_fn()
            for e, outcome in zip(entries, outcomes, strict=True):
                counts[apply_outcome(state, e, outcome, raw_dir, now)] += 1
            progress.remaining = progress.remaining[len(chunk):]
            progress.fetch_counts = dict(counts)
            save_state(state_path, state)


def _write_queue(paths: config.SitePaths, state: SiteState, run_id: str, budget: int | None) -> tuple[Path, int, int, int]:
    for old in paths.staging.iterdir() if paths.staging.exists() else []:
        if old.is_dir() and old.name != run_id:
            shutil.rmtree(old, ignore_errors=True)
    run_dir = run_staging(paths, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    now_pages, deferred = build_queue(list(state.pages.values()), budget)
    items = [
        QueueItem(
            slug=p.slug,
            key=p.key,
            loc=p.loc,
            raw_path=str((paths.raw / f"{p.slug}.md").resolve()),
            raw_hash=p.content_hash or "",
            current_topic=p.topic,
            is_update=p.compiled_hash is not None,
        )
        for p in now_pages
    ]
    queue_path = run_dir / QUEUE_FILE
    atomic_write_json(queue_path, [i.model_dump() for i in items])
    churning = sum(1 for p in now_pages + deferred if p.churning)
    return queue_path, len(items), len(deferred), churning


def prepare(
    root: Path,
    site_id: str,
    run_id: str | None = None,
    *,
    full: bool = False,
    approve_archive: bool = False,
    budget: int | None = None,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    policy: http.HttpPolicy | None = None,
    sleep: http.Sleep | None = None,
    now_fn=config.utcnow,
) -> PrepareResult:
    ws_paths = config.WorkspacePaths(root)
    site = config.get_site(config.load_workspace(root), site_id)
    if site.is_local or not site.sitemap_url:
        raise config.ConfigError(f"'{site_id}' has no sitemap; use `kb ingest` for local files.")

    if run_id is None:
        run_id = locks.acquire(ws_paths.lock_file, label=f"sync {site_id}")
    else:
        locks.verify(ws_paths.lock_file, run_id)

    paths = ws_paths.site(site_id)
    paths.create()
    state = load_state(paths.state_file)
    policy = policy or http.HttpPolicy()
    deadline = _clock() + time_budget_s

    if state.progress is None or state.progress.run_id != run_id:
        result = asyncio.run(_discover(site.sitemap_url, policy, sleep))
        if not result.ok:
            return PrepareResult(
                status="failed",
                site=site_id,
                run_id=run_id,
                message=f"Sitemap read failed at {result.failed_url}: {result.error}. Nothing was changed.",
            )
        _start_run(state, run_id, result.entries, full, approve_archive)
        save_state(paths.state_file, state)

    progress = state.progress
    assert progress is not None
    if progress.remaining:
        asyncio.run(_fetch_chunks(state, paths.state_file, paths.raw, policy, deadline, sleep, now_fn))
        locks.verify(ws_paths.lock_file, run_id)

    base = {
        "site": site_id,
        "run_id": run_id,
        "diff": progress.diff_counts,
        "fetched": progress.fetch_counts,
        "archive_pending": len(progress.removed),
        "archive_held": state.held_archive,
    }
    if progress.remaining:
        return PrepareResult(
            status="incomplete",
            remaining_to_fetch=len(progress.remaining),
            message="Time budget reached; run prepare again with --run-id to continue.",
            **base,
        )

    progress.fetch_done = True
    queue_path, queued, deferred, churning = _write_queue(paths, state, run_id, budget)
    save_state(paths.state_file, state)
    if not site.first_sync_complete:
        site.first_sync_complete = True
        config.update_site(root, site)

    return PrepareResult(
        status="ready",
        queue_size=queued,
        deferred=deferred,
        churning=churning,
        estimated_cost_usd=round(queued * COST_PER_PAGE_USD, 2),
        queue_file=str(queue_path),
        **base,
    )


async def _discover(url: str, policy: http.HttpPolicy, sleep: http.Sleep | None):
    async with http.create_client(policy) as client:
        return await discover(client, url, policy, sleep=sleep)


def abort(root: Path, run_id: str) -> bool:
    """Release the lock and drop this run's staging and in-flight progress."""
    ws_paths = config.WorkspacePaths(root)
    ws = config.load_workspace(root)
    for site_id in ws.sites:
        paths = ws_paths.site(site_id)
        shutil.rmtree(run_staging(paths, run_id), ignore_errors=True)
        if paths.state_file.exists():
            state = load_state(paths.state_file)
            if state.progress and state.progress.run_id == run_id:
                state.progress = None
                save_state(paths.state_file, state)
    return locks.release(ws_paths.lock_file, run_id)


def remaining_budget(root: Path, used: int) -> int:
    return max(config.load_workspace(root).unattended_cap - used, 0)
