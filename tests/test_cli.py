from pathlib import Path

from typer.testing import CliRunner

from kb.cli import app

runner = CliRunner()


def test_help_lists_command_groups() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("site", "sync", "qmd", "ingest", "status"):
        assert name in result.output


def test_status_on_empty_workspace(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--root", str(tmp_path), "status"])
    assert result.exit_code == 0
    assert "No sites registered" in result.output


def test_missing_workspace_gives_guidance() -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2
    assert "kb workspace set" in result.output


def test_workspace_set_then_commands_use_it(tmp_path: Path) -> None:
    target = tmp_path / "kb"
    assert runner.invoke(app, ["workspace", "set", str(target)]).exit_code == 0
    assert target.is_dir()
    assert runner.invoke(app, ["workspace", "show"]).output.strip() == str(target.resolve())
    assert "No sites registered" in runner.invoke(app, ["status"]).output


def test_explicit_root_overrides_saved_workspace(tmp_path: Path) -> None:
    runner.invoke(app, ["workspace", "set", str(tmp_path / "saved")])
    other = tmp_path / "other"
    assert runner.invoke(app, ["--root", str(other), "workspace", "show"]).output.strip() == str(other.resolve())
