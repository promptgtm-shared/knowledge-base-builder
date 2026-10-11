"""Workspace layout and the site registry (sites.json)."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from kb.fsutil import atomic_write_json
from kb.urls import normalize_url, site_id_from_url

LOCAL_SITE_ID = "local"
SITES_FILE = "sites.json"

DEFAULT_TAXONOMY: dict[str, str] = {
    "product-and-features": "Product & Features",
    "guides-and-how-to": "Guides & How-To",
    "industry-and-trends": "Industry & Trends",
    "strategy-and-leadership": "Strategy & Leadership",
    "customers-and-case-studies": "Customers & Case Studies",
    "company-and-news": "Company & News",
    "reference-and-glossary": "Reference & Glossary",
    "general": "General",
}


class ConfigError(Exception):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class Site(BaseModel):
    id: str
    sitemap_url: str | None = None
    description: str = ""
    taxonomy: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TAXONOMY))
    scheduled: bool = False
    first_sync_complete: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    last_sync_at: datetime | None = None

    @property
    def is_local(self) -> bool:
        return self.id == LOCAL_SITE_ID


class Workspace(BaseModel):
    version: int = 1
    unattended_cap: int = 50
    sites: dict[str, Site] = Field(default_factory=dict)


@dataclass(frozen=True)
class SitePaths:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def wiki(self) -> Path:
        return self.root / "wiki"

    @property
    def archive(self) -> Path:
        return self.root / "archive"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    def create(self) -> None:
        for d in (self.raw, self.wiki, self.archive, self.staging, self.runs):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path

    @property
    def sites_file(self) -> Path:
        return self.root / SITES_FILE

    @property
    def lock_file(self) -> Path:
        return self.root / "sync.lock"

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    def site(self, site_id: str) -> SitePaths:
        return SitePaths(self.root / "sites" / site_id)


def load_workspace(root: Path) -> Workspace:
    path = WorkspacePaths(root).sites_file
    if not path.exists():
        return Workspace()
    return Workspace.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_workspace(root: Path, ws: Workspace) -> None:
    atomic_write_json(WorkspacePaths(root).sites_file, ws.model_dump(mode="json"))


def get_site(ws: Workspace, site_id: str) -> Site:
    try:
        return ws.sites[site_id]
    except KeyError:
        raise ConfigError(f"Unknown site '{site_id}'. Registered: {', '.join(sorted(ws.sites)) or 'none'}") from None


def add_site(root: Path, sitemap_url: str, site_id: str | None = None, description: str = "") -> Site:
    ws = load_workspace(root)
    sitemap_url = sitemap_url.strip()
    if not sitemap_url.lower().startswith(("http://", "https://")):
        raise ConfigError(f"Sitemap URL must be http(s): {sitemap_url}")

    site_id = site_id or site_id_from_url(sitemap_url)
    if site_id == LOCAL_SITE_ID:
        raise ConfigError(f"'{LOCAL_SITE_ID}' is reserved for ingested local files.")
    if site_id in ws.sites:
        raise ConfigError(f"Site id '{site_id}' already exists.")
    key = normalize_url(sitemap_url)
    for existing in ws.sites.values():
        if existing.sitemap_url and normalize_url(existing.sitemap_url) == key:
            raise ConfigError(f"Sitemap already registered as '{existing.id}'.")

    site = Site(id=site_id, sitemap_url=sitemap_url, description=description)
    ws.sites[site_id] = site
    WorkspacePaths(root).site(site_id).create()
    save_workspace(root, ws)
    return site


def ensure_local_site(root: Path) -> Site:
    ws = load_workspace(root)
    if LOCAL_SITE_ID not in ws.sites:
        ws.sites[LOCAL_SITE_ID] = Site(id=LOCAL_SITE_ID, description="Local files ingested from input/")
        save_workspace(root, ws)
    WorkspacePaths(root).site(LOCAL_SITE_ID).create()
    WorkspacePaths(root).input_dir.mkdir(parents=True, exist_ok=True)
    return ws.sites[LOCAL_SITE_ID]


def update_site(root: Path, site: Site) -> None:
    ws = load_workspace(root)
    get_site(ws, site.id)
    ws.sites[site.id] = site
    save_workspace(root, ws)


def remove_site(root: Path, site_id: str, purge: bool = False) -> Site:
    ws = load_workspace(root)
    site = get_site(ws, site_id)
    del ws.sites[site_id]
    save_workspace(root, ws)
    if purge:
        shutil.rmtree(WorkspacePaths(root).site(site_id).root, ignore_errors=True)
    return site
