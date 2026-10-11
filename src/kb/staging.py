"""The run staging contract shared by prepare, compile agents and finalize.

staging/<run_id>/
  queue.json            pages queued by prepare (QueueItem list)
  <slug>.md             article written by a compile agent
  manifest-<n>.json     [{"slug", "topic"}] written by the agent for batch n
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from kb import config
from kb.fsutil import atomic_write_json

QUEUE_FILE = "queue.json"
MANIFEST_GLOB = "manifest-*.json"
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class QueueItem(BaseModel):
    slug: str
    key: str
    loc: str
    raw_path: str
    raw_hash: str


def validate_run_id(run_id: str) -> str:
    """Run ids become directory names that get deleted; never accept anything path-like."""
    if not RUN_ID_RE.match(run_id or ""):
        raise config.ConfigError(f"Invalid run id {run_id!r}: expected 12 hex characters as printed by `sync prepare`.")
    return run_id


def run_dir(paths: config.SitePaths, run_id: str) -> Path:
    return paths.staging / validate_run_id(run_id)


def manifest_path(directory: Path, batch: int) -> Path:
    return directory / f"manifest-{batch}.json"


def article_path(directory: Path, slug: str) -> Path:
    return directory / f"{slug}.md"


def write_queue(directory: Path, items: list[QueueItem]) -> Path:
    path = directory / QUEUE_FILE
    atomic_write_json(path, [i.model_dump() for i in items])
    return path


def load_queue(directory: Path) -> list[QueueItem]:
    queue_file = directory / QUEUE_FILE
    if not queue_file.exists():
        return []
    return [QueueItem.model_validate(q) for q in json.loads(queue_file.read_text(encoding="utf-8"))]


def load_manifests(directory: Path) -> dict[str, str]:
    """slug -> topic from every batch manifest; unreadable manifests are skipped."""
    topics: dict[str, str] = {}
    for manifest in sorted(directory.glob(MANIFEST_GLOB)):
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for item in entries if isinstance(entries, list) else []:
            if isinstance(item, dict) and item.get("slug") and item.get("topic"):
                topics[str(item["slug"])] = str(item["topic"]).strip().strip("/")
    return topics
