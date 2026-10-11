import json
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from kb.state import Page, PageStatus, SiteState, load_state, save_state


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = SiteState()
    state.pages["https://example.com/a"] = Page(
        key="https://example.com/a",
        loc="https://example.com/a",
        slug="a",
        status=PageStatus.COMPILED,
        lastmod=datetime(2026, 1, 1, tzinfo=UTC),
        topic="general",
        compiled_hash="h",
    )
    state.qmd_dirty = True
    save_state(path, state)
    loaded = load_state(path)
    assert loaded == state
    assert loaded.pages["https://example.com/a"].article_rel == "general/a.md"
    assert loaded.pages["https://example.com/a"].has_article


def test_missing_file_is_empty_state(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json").pages == {}


def test_newer_version_refused(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99, "pages": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="newer"):
        load_state(path)


def test_crash_mid_write_keeps_previous_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, SiteState(qmd_dirty=False))
    before = path.read_text(encoding="utf-8")
    with mock.patch("kb.fsutil.os.replace", side_effect=OSError("disk gone")):
        with pytest.raises(OSError):
            save_state(path, SiteState(qmd_dirty=True))
    assert path.read_text(encoding="utf-8") == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_churning_flag() -> None:
    assert Page(key="k", loc="k", slug="s", change_streak=3).churning
    assert not Page(key="k", loc="k", slug="s", change_streak=2).churning
