from datetime import timedelta
from pathlib import Path

import pytest

from kb import locks
from kb.config import utcnow


def test_lock_held_across_separate_invocations_of_same_run(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    run_id = locks.acquire(path, label="prepare")
    # finalize / qmd refresh run as new processes: ownership is the token, not the PID
    locks.verify(path, run_id)
    locks.verify(path, run_id)
    with pytest.raises(locks.LockHeld):
        locks.acquire(path)
    assert locks.release(path, run_id)
    assert not path.exists()


def test_wrong_run_id_cannot_verify_or_release(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    run_id = locks.acquire(path)
    with pytest.raises(locks.LockLost):
        locks.verify(path, "someone-else")
    assert not locks.release(path, "someone-else")
    assert path.exists()
    locks.release(path, run_id)


def test_stale_lock_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    old = locks.acquire(path, now=utcnow() - timedelta(hours=7))
    new = locks.acquire(path)
    assert new != old
    with pytest.raises(locks.LockLost):
        locks.verify(path, old)


def test_heartbeat_keeps_lock_fresh(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    run_id = locks.acquire(path, now=utcnow() - timedelta(hours=10))
    locks.verify(path, run_id, now=utcnow() - timedelta(minutes=5))
    with pytest.raises(locks.LockHeld):
        locks.acquire(path)


def test_corrupt_lock_is_treated_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "sync.lock"
    path.write_text("{not json", encoding="utf-8")
    assert locks.acquire(path)


def test_verify_without_lock_raises(tmp_path: Path) -> None:
    with pytest.raises(locks.LockLost):
        locks.verify(tmp_path / "sync.lock", "abc")
