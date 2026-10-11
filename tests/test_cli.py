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
