"""Sync orchestration steps called by the CLI: prepare (resumable), ingest, abort, batches, status."""

from __future__ import annotations

import asyncio
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

from kb import config, http, locks, staging
from kb.convert import document_to_markdown
from kb.diff import approval_matches, build_queue, classify
from kb.fetch import apply_outcome, content_hash, fetch_one, raw_document, settle_status
from kb.fsutil import atomic_write_text
from kb.sitemap import SitemapEntry, SitemapResult, discover
from kb.state import (
    FetchProgress,
    Page,
    PageStatus,
    PendingFetch,
    SiteState,
    load_state,
    save_state,
)
from kb.urls import assign_slug, local_key

COST_PER_PAGE_USD = 0.004
# Well under Claude Code's default 2-minute Bash timeout; long work resumes with --run-id
DEFAULT_TIME_BUDGET_S = 90.0
CHECKPOINT_S = 30.0

Clock = Callable[[], datetime]


def _clock() -> float:
    return time.monotonic()


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


@contextmanager
def _run_lock(ws_paths: config.WorkspacePaths, run_id: str | None, label: str) -> Iterator[str]:
    """Join `run_id`, or start a run; a run started here is released if the step crashes."""
    if run_id is not None:
        staging.validate_run_id(run_id)
        locks.verify(ws_paths.lock_file, run_id)
        yield run_id
        return
    new_id = locks.acquire(ws_paths.lock_file, label=label)
    try:
        yield new_id
    except BaseException:
        locks.release(ws_paths.lock_file, new_id)
        raise


def _start_run(
    state: SiteState,
    run_id: str,
    entries: list[SitemapEntry],
    full: bool,
    approve_archive: bool,
) -> None:
    diff = classify(state, entries, full=full)
    approved = approve_archive and approval_matches(state, diff.removed)
    hold = diff.archive_held and not approved
    state.held_archive = diff.removed if hold else []
    removed = [] if hold else diff.removed

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
    now_fn: Clock,
) -> None:
    progress = state.progress
    assert progress is not None
    sitemap_keys = set(progress.sitemap_keys)
    chunk_size = max(policy.concurrency * 3, 10)
    counts = Counter(progress.fetch_counts)
    taken = state.taken_slugs()
    last_save = _clock()

    def expired() -> bool:
        return _clock() >= deadline

    async with http.create_client(policy) as client:
        sem = asyncio.Semaphore(policy.concurrency)
        while progress.remaining and not expired():
            chunk = progress.remaining[:chunk_size]
            entries = [SitemapEntry(key=p.key, loc=p.loc, lastmod=p.lastmod) for p in chunk]
            outcomes = await asyncio.gather(
                *(fetch_one(client, sem, e, state.pages.get(e.key), policy, sitemap_keys, sleep, expired) for e in entries)
            )
            now = now_fn()
            deferred = []
            for item, entry, outcome in zip(chunk, entries, outcomes, strict=True):
                if outcome.kind == "deferred":
                    deferred.append(item)
                else:
                    counts[apply_outcome(state, entry, outcome, raw_dir, now, taken)] += 1
            progress.remaining = deferred + progress.remaining[len(chunk):]
            progress.fetch_counts = dict(counts)
            # Checkpoint periodically, not per chunk: state.json is rewritten whole and grows with the site
            if _clock() - last_save >= CHECKPOINT_S or not progress.remaining or expired():
                save_state(state_path, state)
                last_save = _clock()


class QueueSummary(NamedTuple):
    path: Path
    queued: int
    deferred: int
    churning: int


def _write_queue(paths: config.SitePaths, state: SiteState, run_id: str, budget: int | None) -> QueueSummary:
    for old in paths.staging.iterdir() if paths.staging.exists() else []:
        if old.is_dir() and old.name != run_id:
            shutil.rmtree(old, ignore_errors=True)
    run_dir = staging.run_dir(paths, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    # Pages leaving the sitemap this run are archived by finalize; compiling them would waste the cap
    leaving = set(state.progress.removed) if state.progress else set()
    now_pages, deferred = build_queue([p for p in state.pages.values() if p.key not in leaving], budget)
    items = [
        staging.QueueItem(
            slug=p.slug,
            key=p.key,
            loc=p.loc,
            raw_path=str((paths.raw / f"{p.slug}.md").resolve()),
            raw_hash=p.content_hash or "",
        )
        for p in now_pages
    ]
    path = staging.write_queue(run_dir, items)
    churning = sum(1 for p in now_pages + deferred if p.churning)
    return QueueSummary(path, len(items), len(deferred), churning)


def _ready(site_id: str, run_id: str, queue: QueueSummary, **extra) -> PrepareResult:
    return PrepareResult(
        status="ready",
        site=site_id,
        run_id=run_id,
        queue_size=queue.queued,
        deferred=queue.deferred,
        churning=queue.churning,
        estimated_cost_usd=round(queue.queued * COST_PER_PAGE_USD, 2),
        queue_file=str(queue.path),
        **extra,
    )


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
    now_fn: Clock = config.utcnow,
) -> PrepareResult:
    ws_paths = config.WorkspacePaths(root)
    site = config.get_site(config.load_workspace(root), site_id)
    if site.is_local or not site.sitemap_url:
        raise config.ConfigError(f"'{site_id}' has no sitemap; use `kb ingest` for local files.")

    with _run_lock(ws_paths, run_id, f"sync {site_id}") as run_id:
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
            "diff": progress.diff_counts,
            "fetched": progress.fetch_counts,
            "archive_pending": len(progress.removed),
            "archive_held": state.held_archive,
        }
        if progress.remaining:
            return PrepareResult(
                status="incomplete",
                site=site_id,
                run_id=run_id,
                remaining_to_fetch=len(progress.remaining),
                message="Time budget reached; run prepare again with --run-id to continue.",
                **base,
            )

        progress.fetch_done = True
        queue = _write_queue(paths, state, run_id, budget)
        save_state(paths.state_file, state)
        if not site.first_sync_complete:
            site.first_sync_complete = True
            config.update_site(root, site)
        return _ready(site_id, run_id, queue, **base)


async def _discover(url: str, policy: http.HttpPolicy, sleep: http.Sleep | None) -> SitemapResult:
    async with http.create_client(policy) as client:
        return await discover(client, url, policy, sleep=sleep)


def prepare_local(root: Path, run_id: str | None = None, now_fn: Clock = config.utcnow) -> PrepareResult:
    """Ingest input/ files into the reserved local site and queue new or changed ones."""
    ws_paths = config.WorkspacePaths(root)
    config.ensure_local_site(root)

    with _run_lock(ws_paths, run_id, "ingest") as run_id:
        paths = ws_paths.site(config.LOCAL_SITE_ID)
        state = load_state(paths.state_file)
        now = now_fn()
        counts: Counter[str] = Counter()
        keys: list[str] = []
        taken = state.taken_slugs()

        for file in sorted(f for f in ws_paths.input_dir.iterdir() if f.is_file()):
            key = local_key(file.name)
            try:
                md = document_to_markdown(file)
            except Exception as e:  # noqa: BLE001 - one bad document must not stop ingest
                counts["error"] += 1
                if key in state.pages:
                    state.pages[key].last_error = f"convert failed: {e}"
                continue
            if md is None:
                counts["unsupported"] += 1
                continue
            keys.append(key)
            page = state.pages.get(key)
            if page is None:
                page = Page(key=key, loc=file.name, slug=assign_slug(key, taken), first_seen=now)
                taken.add(page.slug)
                state.pages[key] = page
            new_hash = content_hash(md)
            page.last_fetched, page.last_error = now, None
            if new_hash == page.content_hash:
                counts["unchanged"] += 1
                continue
            page.content_hash = new_hash
            atomic_write_text(paths.raw / f"{page.slug}.md", raw_document(md, key, now))
            settle_status(page, now)
            counts["changed"] += 1

        state.progress = FetchProgress(run_id=run_id, sitemap_keys=keys, fetch_done=True, fetch_counts=dict(counts))
        queue = _write_queue(paths, state, run_id, None)
        save_state(paths.state_file, state)
        return _ready(config.LOCAL_SITE_ID, run_id, queue, fetched=dict(counts))


def abort(root: Path, run_id: str) -> bool:
    """Release the lock and drop this run's staging and in-flight progress."""
    staging.validate_run_id(run_id)
    ws_paths = config.WorkspacePaths(root)
    try:
        for site_id in config.load_workspace(root).sites:
            paths = ws_paths.site(site_id)
            shutil.rmtree(staging.run_dir(paths, run_id), ignore_errors=True)
            try:
                state = load_state(paths.state_file)
            except (OSError, ValueError):
                continue
            if state.progress and state.progress.run_id == run_id:
                state.progress = None
                save_state(paths.state_file, state)
    finally:
        released = locks.release(ws_paths.lock_file, run_id)
    return released


def remaining_budget(root: Path, used: int) -> int:
    return max(config.load_workspace(root).unattended_cap - used, 0)


class BatchItem(BaseModel):
    slug: str
    source_url: str
    raw_path: str
    output_path: str


class Batch(BaseModel):
    batch: int
    manifest: str
    items: list[BatchItem]


class BatchPlan(BaseModel):
    site: str
    run_id: str
    staging_dir: str
    taxonomy: dict[str, str]
    total: int
    batches: list[Batch]


def plan_batches(root: Path, site_id: str, run_id: str, size: int = 25) -> BatchPlan:
    """Split this run's queue into compile batches with absolute input/output paths."""
    site = config.get_site(config.load_workspace(root), site_id)
    ws_paths = config.WorkspacePaths(root)
    run_dir = staging.run_dir(ws_paths.site(site_id), run_id)
    if not (run_dir / staging.QUEUE_FILE).exists():
        raise config.ConfigError(f"No queue for run {run_id} on '{site_id}'. Run `kb sync prepare` first.")
    locks.verify(ws_paths.lock_file, run_id)  # also refreshes the heartbeat before the long compile phase
    items = staging.load_queue(run_dir)
    size = max(size, 1)
    batches = [
        Batch(
            batch=n + 1,
            manifest=str(staging.manifest_path(run_dir, n + 1).resolve()),
            items=[
                BatchItem(
                    slug=i.slug,
                    source_url=i.loc,
                    raw_path=i.raw_path,
                    output_path=str(staging.article_path(run_dir, i.slug).resolve()),
                )
                for i in items[start:start + size]
            ],
        )
        for n, start in enumerate(range(0, len(items), size))
    ]
    return BatchPlan(
        site=site_id,
        run_id=run_id,
        staging_dir=str(run_dir.resolve()),
        taxonomy=site.taxonomy,
        total=len(items),
        batches=batches,
    )


class SiteStatus(BaseModel):
    id: str
    source: str
    scheduled: bool
    first_sync_complete: bool
    last_sync_at: str | None
    pages: dict[str, int]
    queued: int
    churning: int
    archive_held: list[str]
    qmd_dirty: bool
    last_run: dict | None


class LockStatus(BaseModel):
    run_id: str
    label: str
    heartbeat_at: str
    stale: bool


class WorkspaceStatus(BaseModel):
    root: str
    unattended_cap: int
    qmd_installed: bool
    lock: LockStatus | None
    sites: list[SiteStatus]


def workspace_status(root: Path) -> WorkspaceStatus:
    ws = config.load_workspace(root)
    sites = []
    for site in ws.sites.values():
        state = load_state(config.WorkspacePaths(root).site(site.id).state_file)
        counts = Counter(p.status.value for p in state.pages.values())
        sites.append(
            SiteStatus(
                id=site.id,
                source=site.sitemap_url or "local files (input/)",
                scheduled=site.scheduled,
                first_sync_complete=site.first_sync_complete,
                last_sync_at=site.last_sync_at.isoformat(timespec="seconds") if site.last_sync_at else None,
                pages=dict(counts),
                queued=counts.get(PageStatus.PENDING.value, 0),
                churning=sum(1 for p in state.pages.values() if p.churning and p.status == PageStatus.PENDING),
                archive_held=state.held_archive,
                qmd_dirty=state.qmd_dirty,
                last_run=state.last_run,
            )
        )
    lock = locks.read_lock(config.WorkspacePaths(root).lock_file)
    lock_status = (
        LockStatus(
            run_id=lock.run_id,
            label=lock.label,
            heartbeat_at=lock.heartbeat_at.isoformat(timespec="seconds"),
            stale=locks.is_stale(lock),
        )
        if lock
        else None
    )
    return WorkspaceStatus(
        root=str(root),
        unattended_cap=ws.unattended_cap,
        qmd_installed=shutil.which("qmd") is not None,
        lock=lock_status,
        sites=sites,
    )
