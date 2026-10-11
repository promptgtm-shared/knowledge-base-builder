"""Per-site sync state (state.json): one record per page, keyed by normalized URL."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from kb.config import utcnow
from kb.fsutil import atomic_write_json

STATE_VERSION = 1
CHURN_THRESHOLD = 3


class PageStatus(StrEnum):
    PENDING = "pending_compile"
    COMPILED = "compiled"
    FETCH_ERROR = "fetch_error"
    ARCHIVED = "archived"
    ALIAS = "alias"


class Page(BaseModel):
    key: str
    loc: str
    slug: str
    status: PageStatus = PageStatus.PENDING
    lastmod: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    compiled_hash: str | None = None
    topic: str | None = None
    final_url: str | None = None
    alias_of: str | None = None
    queued_at: datetime | None = None
    change_streak: int = 0
    first_seen: datetime = Field(default_factory=utcnow)
    last_fetched: datetime | None = None
    last_error: str | None = None

    @property
    def has_article(self) -> bool:
        return self.topic is not None and self.compiled_hash is not None

    @property
    def article_rel(self) -> str | None:
        return f"{self.topic}/{self.slug}.md" if self.topic else None

    @property
    def churning(self) -> bool:
        return self.change_streak >= CHURN_THRESHOLD


class PendingFetch(BaseModel):
    key: str
    loc: str
    lastmod: datetime | None = None


class FetchProgress(BaseModel):
    """Resumable state of a chunked `prepare` run, consumed by `finalize`."""

    run_id: str
    remaining: list[PendingFetch] = Field(default_factory=list)
    sitemap_keys: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    diff_counts: dict[str, int] = Field(default_factory=dict)
    fetch_counts: dict[str, int] = Field(default_factory=dict)
    fetch_done: bool = False
    started_at: datetime = Field(default_factory=utcnow)


class SiteState(BaseModel):
    version: int = STATE_VERSION
    pages: dict[str, Page] = Field(default_factory=dict)
    qmd_dirty: bool = False
    held_archive: list[str] = Field(default_factory=list)
    progress: FetchProgress | None = None
    last_run: dict | None = None

    def taken_slugs(self) -> set[str]:
        return {p.slug for p in self.pages.values()}

    def pending(self) -> list[Page]:
        return [p for p in self.pages.values() if p.status == PageStatus.PENDING]


def load_state(path: Path) -> SiteState:
    if not path.exists():
        return SiteState()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version", STATE_VERSION) > STATE_VERSION:
        raise ValueError(f"{path} was written by a newer kb-builder (version {data['version']}).")
    return SiteState.model_validate(data)


def save_state(path: Path, state: SiteState) -> None:
    atomic_write_json(path, state.model_dump(mode="json"))
