"""Classify a sitemap against stored state, guard mass archives, and order the compile queue."""

from __future__ import annotations

from dataclasses import dataclass, field

from kb.sitemap import SitemapEntry
from kb.state import Page, PageStatus, SiteState

ARCHIVE_GUARD_RATIO = 0.2
ARCHIVE_GUARD_MIN_KNOWN = 10


@dataclass
class DiffResult:
    new: list[SitemapEntry] = field(default_factory=list)
    lastmod_changed: list[SitemapEntry] = field(default_factory=list)
    no_lastmod: list[SitemapEntry] = field(default_factory=list)
    retry: list[SitemapEntry] = field(default_factory=list)
    reappeared: list[SitemapEntry] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    archive_held: bool = False

    def to_fetch(self) -> list[SitemapEntry]:
        return [*self.new, *self.reappeared, *self.retry, *self.lastmod_changed, *self.no_lastmod]

    def counts(self) -> dict[str, int]:
        return {
            "new": len(self.new),
            "lastmod_changed": len(self.lastmod_changed),
            "no_lastmod": len(self.no_lastmod),
            "retry": len(self.retry),
            "reappeared": len(self.reappeared),
            "unchanged": len(self.unchanged),
            "removed": len(self.removed),
        }


def classify(state: SiteState, entries: list[SitemapEntry], full: bool = False) -> DiffResult:
    result = DiffResult()
    seen: set[str] = set()

    for entry in entries:
        seen.add(entry.key)
        page = state.pages.get(entry.key)
        if page is None:
            result.new.append(entry)
        elif page.status == PageStatus.ARCHIVED:
            result.reappeared.append(entry)
        elif page.status == PageStatus.FETCH_ERROR:
            result.retry.append(entry)
        elif full:
            result.lastmod_changed.append(entry)
        elif entry.lastmod is None:
            if page.status == PageStatus.PENDING:
                result.unchanged.append(entry.key)  # raw already on disk, awaiting compile
            else:
                result.no_lastmod.append(entry)
        elif page.lastmod is None or entry.lastmod != page.lastmod:
            result.lastmod_changed.append(entry)
        else:
            result.unchanged.append(entry.key)

    result.removed = sorted(
        key for key, page in state.pages.items() if key not in seen and page.status != PageStatus.ARCHIVED
    )
    result.archive_held = archive_guard_trips(state, result.removed)
    return result


def archive_guard_trips(state: SiteState, removed: list[str]) -> bool:
    """Hold archiving when a large share of known pages vanished at once."""
    known = sum(1 for p in state.pages.values() if p.status not in (PageStatus.ARCHIVED, PageStatus.ALIAS))
    return known >= ARCHIVE_GUARD_MIN_KNOWN and len(removed) > ARCHIVE_GUARD_RATIO * known


def approval_matches(state: SiteState, removed: list[str]) -> bool:
    return bool(state.held_archive) and sorted(state.held_archive) == sorted(removed)


def queue_order(page: Page) -> tuple:
    """Oldest pending first; new before updated on ties; churning pages last."""
    queued = page.queued_at.timestamp() if page.queued_at else 0.0
    return (page.churning, queued, page.compiled_hash is not None, page.key)


def build_queue(pages: list[Page], budget: int | None) -> tuple[list[Page], list[Page]]:
    """Split pending pages into (to compile now, deferred) given a remaining budget."""
    ordered = sorted((p for p in pages if p.status == PageStatus.PENDING), key=queue_order)
    if budget is None:
        return ordered, []
    budget = max(budget, 0)
    return ordered[:budget], ordered[budget:]
