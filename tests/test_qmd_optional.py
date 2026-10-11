"""qmd is optional: without it the wiki is still built and indexing waits for an install."""

import json
from pathlib import Path

from test_qmd import FakeQmd, setup
from typer.testing import CliRunner

from kb import config, locks
from kb.cli import app
from kb.qmd import Qmd, refresh
from kb.state import load_state


def no_qmd(monkeypatch) -> None:
    monkeypatch.setattr("kb.qmd.shutil.which", lambda _: None)
    monkeypatch.setattr("kb.sync.shutil.which", lambda _: None)


def test_missing_qmd_skips_keeps_sites_marked_and_releases_lock(tmp_path: Path, monkeypatch) -> None:
    no_qmd(monkeypatch)
    run_id = setup(tmp_path, {"alpha": True})
    result = refresh(tmp_path, run_id)
    assert result.status == "skipped" and result.qmd_missing and result.still_dirty == ["alpha"]
    assert "npm install -g @tobilu/qmd" in result.message
    assert load_state(config.WorkspacePaths(tmp_path).site("alpha").state_file).qmd_dirty
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_first_run_after_install_indexes_the_backlog(tmp_path: Path, monkeypatch) -> None:
    no_qmd(monkeypatch)
    refresh(tmp_path, setup(tmp_path, {"alpha": True}))

    monkeypatch.undo()
    fake = FakeQmd()
    result = refresh(tmp_path, locks.acquire(config.WorkspacePaths(tmp_path).lock_file), Qmd(fake))
    assert result.status == "done" and result.created_collections == ["kb-alpha"]
    assert not load_state(config.WorkspacePaths(tmp_path).site("alpha").state_file).qmd_dirty


def test_qmd_check_reports_install_commands(monkeypatch) -> None:
    no_qmd(monkeypatch)
    out = json.loads(CliRunner().invoke(app, ["qmd", "check"]).output)
    assert out["installed"] is False and out["version"] is None
    assert out["install_commands"][0] == "npm install -g @tobilu/qmd" and out["models_command"] == "qmd pull"


def test_status_says_search_is_pending(tmp_path: Path, monkeypatch) -> None:
    no_qmd(monkeypatch)
    setup(tmp_path, {"alpha": True})
    runner = CliRunner()
    text = runner.invoke(app, ["--root", str(tmp_path), "status"]).output
    assert "qmd is not installed; indexing is pending" in text
    status = json.loads(runner.invoke(app, ["--root", str(tmp_path), "status", "--json"]).output)
    assert status["qmd_installed"] is False


def test_qmd_check_when_installed() -> None:
    fake = FakeQmd()
    fake_version = Qmd(lambda args, timeout: (0, "qmd 2.8.3 (abc)") if args == ["--version"] else fake(args, timeout))
    from kb.qmd import check

    result = check(fake_version)
    assert result.installed and result.version == "qmd 2.8.3 (abc)"
