"""Workspace run lock owned by a run id (not a PID).

A sync run spans several short-lived CLI calls plus LLM compile time, so
ownership is a token passed between steps and liveness is a heartbeat age.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from kb.config import utcnow
from kb.fsutil import atomic_write_text

STALE_AFTER = timedelta(hours=6)


class Lock(BaseModel):
    run_id: str
    created_at: datetime
    heartbeat_at: datetime
    label: str = ""
    qmd_updated: bool = False


class LockHeld(Exception):
    def __init__(self, lock: Lock) -> None:
        self.lock = lock
        super().__init__(f"Workspace is locked by run {lock.run_id} ({lock.label or 'sync'}), last active {lock.heartbeat_at:%Y-%m-%d %H:%M} UTC.")


class LockLost(Exception):
    pass


def read_lock(path: Path) -> Lock | None:
    try:
        return Lock.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        # Unreadable lock: treat as stale so a crash mid-write can't wedge the workspace
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        return Lock(run_id="corrupt", created_at=epoch, heartbeat_at=epoch)


def is_stale(lock: Lock, now: datetime | None = None) -> bool:
    return (now or utcnow()) - lock.heartbeat_at > STALE_AFTER


def acquire(path: Path, label: str = "", now: datetime | None = None) -> str:
    now = now or utcnow()
    existing = read_lock(path)
    if existing is not None:
        if not is_stale(existing, now):
            raise LockHeld(existing)
        path.unlink(missing_ok=True)

    lock = Lock(run_id=uuid.uuid4().hex[:12], created_at=now, heartbeat_at=now, label=label)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raced = read_lock(path)
        raise LockHeld(raced) if raced else LockLost("Lock contention while acquiring.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(lock.model_dump_json())
    return lock.run_id


def verify(path: Path, run_id: str, now: datetime | None = None, **updates: object) -> Lock:
    """Confirm `run_id` owns the lock, refresh its heartbeat, and apply run-scoped flag updates."""
    lock = read_lock(path)
    if lock is None or lock.run_id != run_id:
        holder = f"run {lock.run_id}" if lock else "nobody"
        raise LockLost(f"Run {run_id} does not hold the workspace lock (held by {holder}).")
    lock = lock.model_copy(update={**updates, "heartbeat_at": now or utcnow()})
    atomic_write_text(path, lock.model_dump_json())
    return lock


def release(path: Path, run_id: str) -> bool:
    lock = read_lock(path)
    if lock is None or lock.run_id != run_id:
        return False
    path.unlink(missing_ok=True)
    return True
