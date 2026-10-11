from datetime import UTC, datetime, timedelta

from kb.diff import approval_matches, build_queue, classify
from kb.sitemap import SitemapEntry
from kb.state import Page, PageStatus, SiteState

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 2, 1, tzinfo=UTC)


def url(i) -> str:
    return f"https://example.com/p{i}"


def entry(i, lastmod=T0) -> SitemapEntry:
    return SitemapEntry(key=url(i), loc=url(i), lastmod=lastmod)


def page(i, status=PageStatus.COMPILED, lastmod=T0, **kw) -> Page:
    defaults = {"topic": "general", "compiled_hash": "h"} if status in (PageStatus.COMPILED, PageStatus.ARCHIVED) else {}
    defaults.update(kw)
    return Page(key=url(i), loc=url(i), slug=f"p{i}", status=status, lastmod=lastmod, **defaults)


def state_with(*pages: Page) -> SiteState:
    return SiteState(pages={p.key: p for p in pages})


def test_empty_state_all_new() -> None:
    result = classify(SiteState(), [entry(i) for i in range(5)])
    assert len(result.new) == 5
    assert len(result.to_fetch()) == 5


def test_same_lastmod_is_unchanged_and_not_fetched() -> None:
    result = classify(state_with(page(1)), [entry(1)])
    assert result.unchanged == [url(1)]
    assert result.to_fetch() == []


def test_changed_lastmod_fetched() -> None:
    result = classify(state_with(page(1)), [entry(1, T1)])
    assert [e.key for e in result.lastmod_changed] == [url(1)]


def test_missing_lastmod_fetched_every_run() -> None:
    result = classify(state_with(page(1, lastmod=None)), [entry(1, None)])
    assert [e.key for e in result.no_lastmod] == [url(1)]


def test_full_forces_fetch() -> None:
    assert len(classify(state_with(page(1)), [entry(1)], full=True).to_fetch()) == 1


def test_pending_from_crashed_run_not_refetched() -> None:
    result = classify(state_with(page(1, status=PageStatus.PENDING)), [entry(1)])
    assert result.to_fetch() == []
    assert result.unchanged == [url(1)]


def test_fetch_error_retried_and_reappeared_detected() -> None:
    s = state_with(page(1, status=PageStatus.FETCH_ERROR), page(2, status=PageStatus.ARCHIVED))
    result = classify(s, [entry(1), entry(2)])
    assert [e.key for e in result.retry] == [url(1)]
    assert [e.key for e in result.reappeared] == [url(2)]


def test_archive_guard_thresholds() -> None:
    hundred = state_with(*(page(i) for i in range(100)))
    held = classify(hundred, [entry(i) for i in range(70)])
    assert len(held.removed) == 30 and held.archive_held

    ok = classify(hundred, [entry(i) for i in range(97)])
    assert len(ok.removed) == 3 and not ok.archive_held

    small = state_with(*(page(i) for i in range(8)))
    below_min = classify(small, [entry(i) for i in range(5)])
    assert len(below_min.removed) == 3 and not below_min.archive_held


def test_archived_pages_not_counted_as_removed() -> None:
    s = state_with(page(1), page(2, status=PageStatus.ARCHIVED))
    assert classify(s, [entry(1)]).removed == []


def test_approval_requires_identical_held_set() -> None:
    s = SiteState(held_archive=[url(1), url(2)])
    assert approval_matches(s, [url(2), url(1)])
    assert not approval_matches(s, [url(1)])
    assert not approval_matches(SiteState(), [])


def test_queue_oldest_first_new_before_updated_churning_last() -> None:
    now = T1
    pages = [
        page("old-update", status=PageStatus.PENDING, queued_at=now - timedelta(days=3), compiled_hash="x"),
        page("new-today", status=PageStatus.PENDING, queued_at=now),
        page("update-today", status=PageStatus.PENDING, queued_at=now, compiled_hash="x"),
        page("churn", status=PageStatus.PENDING, queued_at=now - timedelta(days=10), change_streak=5),
        page("done"),
    ]
    now_list, deferred = build_queue(pages, budget=3)
    assert [p.slug for p in now_list] == ["pold-update", "pnew-today", "pupdate-today"]
    assert [p.slug for p in deferred] == ["pchurn"]


def test_queue_budget_limits_and_unbounded() -> None:
    pages = [page(i, status=PageStatus.PENDING, queued_at=T0 + timedelta(minutes=i)) for i in range(120)]
    first, rest = build_queue(pages, budget=50)
    assert len(first) == 50 and len(rest) == 70
    assert first[0].slug == "p0"
    all_now, none = build_queue(pages, budget=None)
    assert len(all_now) == 120 and none == []
    zero, everything = build_queue(pages, budget=0)
    assert zero == [] and len(everything) == 120
