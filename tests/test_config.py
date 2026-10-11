import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kb import config
from kb.cli import app

runner = CliRunner()


def test_add_site_creates_layout_and_persists(tmp_path: Path) -> None:
    site = config.add_site(tmp_path, "https://www.example.com/sitemap.xml", description="Example")
    assert site.id == "example-com"
    paths = config.WorkspacePaths(tmp_path).site("example-com")
    for d in (paths.raw, paths.wiki, paths.archive, paths.staging, paths.runs):
        assert d.is_dir()
    ws = config.load_workspace(tmp_path)
    assert ws.sites["example-com"].taxonomy["general"] == "General"
    assert ws.unattended_cap == 50


def test_duplicate_sitemap_and_reserved_id_rejected(tmp_path: Path) -> None:
    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    with pytest.raises(config.ConfigError, match="already registered"):
        config.add_site(tmp_path, "HTTPS://example.com/sitemap.xml", site_id="other")
    with pytest.raises(config.ConfigError, match="reserved"):
        config.add_site(tmp_path, "https://other.com/sitemap.xml", site_id="local")
    with pytest.raises(config.ConfigError, match="http"):
        config.add_site(tmp_path, "ftp://x.com/sitemap.xml")


def test_remove_keeps_files_unless_purge(tmp_path: Path) -> None:
    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    root = config.WorkspacePaths(tmp_path).site("example-com").root
    config.remove_site(tmp_path, "example-com")
    assert root.exists()
    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    config.remove_site(tmp_path, "example-com", purge=True)
    assert not root.exists()


def test_cli_site_lifecycle(tmp_path: Path) -> None:
    base = ["--root", str(tmp_path)]
    assert runner.invoke(app, [*base, "site", "add", "https://example.com/sitemap.xml"]).exit_code == 0
    shown = runner.invoke(app, [*base, "site", "show", "example-com"])
    assert json.loads(shown.output)["sitemap_url"] == "https://example.com/sitemap.xml"

    tax = tmp_path / "tax.json"
    tax.write_text(json.dumps({"pricing": "Pricing"}), encoding="utf-8")
    assert runner.invoke(app, [*base, "site", "update", "example-com", "--taxonomy-file", str(tax)]).exit_code == 0
    assert config.load_workspace(tmp_path).sites["example-com"].taxonomy == {"pricing": "Pricing", "general": "General"}

    refused = runner.invoke(app, [*base, "site", "update", "example-com", "--scheduled"])
    assert refused.exit_code == 2
    assert "first sync" in refused.output

    dup = runner.invoke(app, [*base, "site", "add", "https://example.com/sitemap.xml"])
    assert dup.exit_code == 2
    assert "list" in runner.invoke(app, [*base, "site", "--help"]).output


def test_ensure_local_site(tmp_path: Path) -> None:
    site = config.ensure_local_site(tmp_path)
    assert site.is_local and site.sitemap_url is None
    assert (tmp_path / "input").is_dir()
    assert config.ensure_local_site(tmp_path).id == "local"


def test_cli_site_list_json_filters(tmp_path: Path) -> None:
    base = ["--root", str(tmp_path)]
    config.add_site(tmp_path, "https://a.example/sitemap.xml", site_id="a")
    config.add_site(tmp_path, "https://b.example/sitemap.xml", site_id="b")
    config.ensure_local_site(tmp_path)
    ws = config.load_workspace(tmp_path)
    ws.sites["b"].first_sync_complete = True
    ws.sites["b"].scheduled = True
    config.save_workspace(tmp_path, ws)

    def ids(*flags: str) -> list[str]:
        return [s["id"] for s in json.loads(runner.invoke(app, [*base, "site", "list", "--json", *flags]).output)]

    assert ids() == ["a", "b", "local"]
    assert ids("--sitemap-only") == ["a", "b"]
    assert ids("--sitemap-only", "--scheduled") == ["b"]
