"""Finalize a run: publish staged articles, archive/restore, reconcile, rebuild indexes, report."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from kb import config, indexes, locks
from kb.fsutil import atomic_write_text
from kb.report import write_run_report
from kb.staging import QueueItem, article_path, load_manifests, load_queue, run_dir
from kb.state import Page, PageStatus, SiteState, load_state, save_state
from kb.urls import is_local_key, normalize_url

MIN_ARTICLE_CHARS = 200
HEADER_RE = re.compile(r"^\*\*(Source|Created|Updated|Topics):\*\*\s*(.*)$")


class FinalizeError(Exception):
    pass


class FinalizeResult(BaseModel):
    site: str
    run_id: str
    status: str
    compiled: int = 0
    failed: list[str] = Field(default_factory=list)
    archived: int = 0
    restored: int = 0
    requeued: int = 0
    dropped: int = 0
    archive_held: int = 0
    pending_after: int = 0
    churning: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)
    reconciled: list[str] = Field(default_factory=list)
    qmd_dirty: bool = False
    report: str = ""


def read_header(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 15:
                    break
                m = HEADER_RE.match(line.strip())
                if m:
                    fields[m.group(1)] = m.group(2).strip()
    except OSError:
        pass
    return fields


def render_article(body: str, source: str, topic_label: str, created: str, updated: str | None) -> str:
    """Normalize an agent-written article: title first, then the canonical header lines."""
    lines = [ln for ln in body.replace("\r\n", "\n").lstrip("﻿").strip().split("\n") if not HEADER_RE.match(ln.strip())]
    title_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
    if title_idx is None:
        raise FinalizeError("article has no '# Title' heading")
    title, rest = lines[title_idx], lines[title_idx + 1:]
    header = [f"**Source:** {source}", f"**Created:** {created}"]
    if updated:
        header.append(f"**Updated:** {updated}")
    header.append(f"**Topics:** {topic_label}")
    body_text = re.sub(r"\n{3,}", "\n\n", "\n".join(rest)).strip()
    return f"{title}\n\n" + "\n".join(header) + f"\n\n{body_text}\n"


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _remove_stale_copies(paths: config.SitePaths, slug: str, keep: Path) -> None:
    for base in (paths.wiki, paths.archive):
        for candidate in base.glob(f"*/{slug}.md"):
            if candidate.resolve() != keep.resolve():
                candidate.unlink(missing_ok=True)


def _publish(
    paths: config.SitePaths,
    page: Page,
    item: QueueItem,
    staged: Path,
    topic: str,
    taxonomy: dict[str, str],
    today: str,
) -> None:
    body = staged.read_text(encoding="utf-8")
    if len(body.strip()) < MIN_ARTICLE_CHARS:
        raise FinalizeError("staged article is too short")
    previous = [base / page.article_rel for base in (paths.wiki, paths.archive)] if page.article_rel else []
    created = next((read_header(p).get("Created") for p in previous if p.exists()), None)
    source = page.loc if not is_local_key(page.key) else page.key
    article = render_article(body, source, taxonomy.get(topic, topic), created or today, today if created else None)

    target = paths.wiki / topic / f"{page.slug}.md"
    atomic_write_text(target, article)
    _remove_stale_copies(paths, page.slug, target)

    page.topic = topic
    page.compiled_hash = item.raw_hash
    page.status = PageStatus.COMPILED
    page.queued_at = None


def _archive(paths: config.SitePaths, state: SiteState, keys: list[str]) -> tuple[int, int]:
    archived = dropped = 0
    for key in keys:
        page = state.pages.get(key)
        if page is None:
            continue
        if page.has_article:
            src = paths.wiki / page.article_rel
            if src.exists():
                _move(src, paths.archive / page.article_rel)
            page.status = PageStatus.ARCHIVED
            page.queued_at = None
            archived += 1
        else:
            (paths.raw / f"{page.slug}.md").unlink(missing_ok=True)
            del state.pages[key]
            dropped += 1
    return archived, dropped


def _archive_aliases(paths: config.SitePaths, state: SiteState) -> int:
    moved = 0
    for page in state.pages.values():
        if page.status == PageStatus.ALIAS and page.article_rel and (paths.wiki / page.article_rel).exists():
            _move(paths.wiki / page.article_rel, paths.archive / page.article_rel)
            moved += 1
    return moved


def _should_be_in_wiki(page: Page, sitemap_keys: set[str]) -> bool:
    if not page.has_article:
        return False
    if page.status == PageStatus.ARCHIVED:
        # Reappeared with unchanged content: restore without recompiling
        return page.key in sitemap_keys and page.content_hash == page.compiled_hash
    if page.alias_of:
        return False  # a redirect duplicate's article belongs in archive/
    return page.status in (PageStatus.COMPILED, PageStatus.FETCH_ERROR)


def _heal(paths: config.SitePaths, state: SiteState, sitemap_keys: set[str], now: datetime) -> tuple[int, int]:
    """Every page that should have a wiki article gets one: restore it from archive/, or requeue it."""
    restored = requeued = 0
    for page in state.pages.values():
        if not _should_be_in_wiki(page, sitemap_keys):
            continue
        wiki_copy = paths.wiki / page.article_rel
        archive_copy = paths.archive / page.article_rel
        if not wiki_copy.exists():
            if archive_copy.exists():
                _move(archive_copy, wiki_copy)
                restored += 1
            else:
                # The article is gone: forget it was compiled so a same-content refetch cannot mark it done
                page.compiled_hash = None
                if (paths.raw / f"{page.slug}.md").exists():
                    page.status, page.queued_at = PageStatus.PENDING, now
                else:
                    # Nothing to compile from: the next prepare retries the fetch, then queues it
                    page.status = PageStatus.FETCH_ERROR
                    page.content_hash = page.lastmod = page.etag = page.last_modified = None
                requeued += 1
                continue
        page.status = PageStatus.COMPILED if page.status == PageStatus.ARCHIVED else page.status
    return restored, requeued


def _expected_wiki_path(page: Page) -> str | None:
    if page.status in (PageStatus.ARCHIVED, PageStatus.ALIAS):
        return None
    return page.article_rel if page.has_article else None


def _reconcile(paths: config.SitePaths, state: SiteState) -> tuple[list[str], list[str]]:
    """Delete only stale copies of tracked pages; report untracked files, never delete them."""
    removed: list[str] = []
    untracked: list[str] = []
    for topic_dir in (d for d in paths.wiki.iterdir() if d.is_dir()) if paths.wiki.exists() else []:
        for f in topic_dir.glob("*.md"):
            if not indexes.is_article(f):
                continue
            rel = f"{topic_dir.name}/{f.name}"
            source = read_header(f).get("Source")
            key = source if source and is_local_key(source) else (normalize_url(source) if source else None)
            page = state.pages.get(key) if key else None
            if page is None:
                untracked.append(rel)
            elif _expected_wiki_path(page) != rel:
                f.unlink(missing_ok=True)
                removed.append(rel)
        contents = list(topic_dir.iterdir())
        if all(p.name == indexes.TOPIC_INDEX for p in contents):
            for index_file in contents:
                index_file.unlink(missing_ok=True)
            topic_dir.rmdir()
    return removed, untracked


def finalize(root: Path, site_id: str, run_id: str, now: datetime | None = None) -> FinalizeResult:
    now = now or config.utcnow()
    ws_paths = config.WorkspacePaths(root)
    locks.verify(ws_paths.lock_file, run_id)
    site = config.get_site(config.load_workspace(root), site_id)
    paths = ws_paths.site(site_id)
    state = load_state(paths.state_file)

    progress = state.progress
    if progress is None or progress.run_id != run_id or not progress.fetch_done:
        raise FinalizeError(f"No completed prepare for run {run_id} on '{site_id}'. Run `kb sync prepare` first.")

    staged_dir = run_dir(paths, run_id)
    queue = load_queue(staged_dir)
    topics = load_manifests(staged_dir)
    today = now.date().isoformat()
    result = FinalizeResult(site=site_id, run_id=run_id, status="ok")

    for item in queue:
        page = state.pages.get(item.key)
        if page is None or page.status != PageStatus.PENDING or page.content_hash != item.raw_hash:
            continue  # stale queue entry: page changed or was handled since this run queued it
        staged = article_path(staged_dir, item.slug)
        if not staged.exists():
            result.failed.append(f"{item.slug}: no staged article")
            continue
        # A batch whose agent died before writing its manifest keeps the page's previous topic
        topic = topics.get(item.slug) or page.topic or "general"
        if topic not in site.taxonomy:
            topic = "general"
        try:
            _publish(paths, page, item, staged, topic, site.taxonomy, today)
            result.compiled += 1
        except (FinalizeError, OSError, ValueError) as e:
            result.failed.append(f"{item.slug}: {e}")
    # Record publishes before moving anything else, so a later failure can't orphan them
    save_state(paths.state_file, state)

    result.archived, result.dropped = _archive(paths, state, progress.removed)
    result.archived += _archive_aliases(paths, state)
    result.restored, result.requeued = _heal(paths, state, set(progress.sitemap_keys), now)
    result.reconciled, result.untracked = _reconcile(paths, state)

    changed = result.compiled + result.archived + result.restored + len(result.reconciled)
    counts = indexes.rebuild(paths.wiki, site.description or site.id, site.sitemap_url, site.taxonomy, now.date())
    if changed:
        state.qmd_dirty = True

    result.archive_held = len(state.held_archive)
    pending = state.pending()
    result.pending_after = len(pending)
    result.churning = sorted(p.loc for p in state.pages.values() if p.churning and p.status == PageStatus.PENDING)
    result.qmd_dirty = state.qmd_dirty
    if result.failed or result.pending_after:
        result.status = "partial"

    errors = sorted(f"{p.loc}: {p.last_error}" for p in state.pages.values() if p.last_error)
    notes = []
    if state.held_archive:
        notes.append(
            f"{len(state.held_archive)} removed pages are held by the mass-archive guard. "
            f"Review them and run an interactive sync with --approve-archive to archive them."
        )
    report = write_run_report(
        paths.runs,
        site_id,
        run_id,
        now,
        result.status,
        {
            **{f"sitemap {k}": v for k, v in progress.diff_counts.items()},
            **{f"fetch {k}": v for k, v in progress.fetch_counts.items()},
            "compiled": result.compiled,
            "compile failures": len(result.failed),
            "archived": result.archived,
            "restored": result.restored,
            "still queued": result.pending_after,
            "articles in wiki": sum(counts.values()),
        },
        {
            "Compile failures": result.failed,
            "Churning pages (content changes every sync)": result.churning,
            "Held archive (guard)": state.held_archive,
            "Fetch errors": errors,
            "Untracked wiki files (left in place)": result.untracked,
            "Stale copies removed": result.reconciled,
        },
        notes,
    )
    result.report = str(report)

    state.last_run = {"run_id": run_id, "finished": now.isoformat(), "status": result.status, "report": str(report)}
    state.progress = None
    save_state(paths.state_file, state)
    site.last_sync_at = now
    config.update_site(root, site)
    return result
