"""End-to-end sync cycles: prepare -> simulated compile agents -> finalize -> qmd refresh."""

import json
from pathlib import Path

import httpx
import respx
from test_qmd import FakeQmd
from typer.testing import CliRunner

from kb import config, locks, sync
from kb.cli import app
from kb.finalize import finalize
from kb.http import HttpPolicy
from kb.qmd import Qmd, refresh
from kb.state import PageStatus, load_state

SITEMAP = "https://example.com/sitemap.xml"
POLICY = HttpPolicy(max_retries=1, concurrency=2)
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
ARTICLE = "# {title}\n\n## Summary\n\n" + "This article summarizes the source page in a few sentences. " * 5 + "\n"


async def _no_sleep(_: float) -> None:
    return None


def html(title: str) -> str:
    return f"<html><body><article><h1>{title}</h1><p>{'Body text about the topic. ' * 10}</p></article></body></html>"


def serve(pages: dict[str, tuple[str, str]]) -> None:
    """pages: slug -> (lastmod, title)"""
    items = "".join(f"<url><loc>https://example.com/{s}</loc><lastmod>{m}</lastmod></url>" for s, (m, _) in pages.items())
    respx.get(SITEMAP).mock(return_value=httpx.Response(200, text=f"<urlset {NS}>{items}</urlset>"))
    for slug, (_, title) in pages.items():
        respx.get(f"https://example.com/{slug}").mock(return_value=httpx.Response(200, html=html(title)))


def fake_agents(root: Path, run_id: str, topic: str = "general") -> list[str]:
    """Do what compile agents do: read each batch, write staged articles + manifest."""
    plan = sync.plan_batches(root, "example-com", run_id, size=2)
    compiled = []
    for batch in plan.batches:
        manifest = []
        for item in batch.items:
            title = Path(item.raw_path).read_text(encoding="utf-8").split("# ", 1)[1].splitlines()[0]
            Path(item.output_path).write_text(ARTICLE.format(title=title), encoding="utf-8")
            manifest.append({"slug": item.slug, "topic": topic})
            compiled.append(item.slug)
        Path(batch.manifest).write_text(json.dumps(manifest), encoding="utf-8")
    return compiled


def cycle(root: Path, fake_qmd: FakeQmd, **prepare_kw):
    prepared = sync.prepare(root, "example-com", policy=POLICY, sleep=_no_sleep, **prepare_kw)
    assert prepared.status == "ready", prepared
    compiled = fake_agents(root, prepared.run_id)
    finalized = finalize(root, "example-com", prepared.run_id)
    refreshed = refresh(root, prepared.run_id, Qmd(fake_qmd))
    return prepared, compiled, finalized, refreshed


@respx.mock
def test_two_sync_cycles_only_recompile_changed_page(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP, description="Example docs")
    wiki = config.WorkspacePaths(tmp_path).site("example-com").wiki

    serve({"a": ("2026-01-01", "Alpha"), "b": ("2026-01-01", "Beta"), "c": ("2026-01-01", "Gamma")})
    qmd1 = FakeQmd()
    prepared, compiled, finalized, refreshed = cycle(tmp_path, qmd1)
    assert sorted(compiled) == ["a", "b", "c"]
    assert finalized.compiled == 3 and finalized.status == "ok"
    assert refreshed.status == "done" and refreshed.created_collections == ["kb-example-com"]
    assert (wiki / "general" / "a.md").read_text(encoding="utf-8").startswith("# Alpha\n\n**Source:** https://example.com/a\n")
    assert "| [General](general/_index.md) | 3 |" in (wiki / "_master-index.md").read_text(encoding="utf-8")
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None

    respx.reset()
    serve({"a": ("2026-01-01", "Alpha"), "b": ("2026-02-01", "Beta v2"), "d": ("2026-02-01", "Delta")})
    qmd2 = FakeQmd(existing={"kb-example-com"})
    prepared2, compiled2, finalized2, refreshed2 = cycle(tmp_path, qmd2)
    assert prepared2.diff["new"] == 1 and prepared2.diff["lastmod_changed"] == 1 and prepared2.diff["removed"] == 1
    assert sorted(compiled2) == ["b", "d"]
    assert finalized2.compiled == 2 and finalized2.archived == 1
    assert respx.get("https://example.com/a").call_count == 0
    assert (wiki / "general" / "b.md").read_text(encoding="utf-8").startswith("# Beta v2")
    assert "**Updated:**" in (wiki / "general" / "b.md").read_text(encoding="utf-8")
    assert not (wiki / "general" / "c.md").exists()
    assert (config.WorkspacePaths(tmp_path).site("example-com").archive / "general" / "c.md").exists()
    assert qmd2.commands() == ["collection show", "update", "embed"]

    respx.reset()
    serve({"a": ("2026-01-01", "Alpha"), "b": ("2026-02-01", "Beta v2"), "d": ("2026-02-01", "Delta")})
    qmd3 = FakeQmd(existing={"kb-example-com"})
    prepared3 = sync.prepare(tmp_path, "example-com", policy=POLICY, sleep=_no_sleep)
    assert prepared3.queue_size == 0
    finalize(tmp_path, "example-com", prepared3.run_id)
    assert refresh(tmp_path, prepared3.run_id, Qmd(qmd3)).status == "skipped"
    assert qmd3.calls == []


@respx.mock
def test_unattended_cap_defers_rest_to_next_run(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    serve({f"p{i}": ("2026-01-01", f"Page {i}") for i in range(5)})
    prepared, compiled, finalized, _ = cycle(tmp_path, FakeQmd(), budget=3)
    assert len(compiled) == 3 and prepared.deferred == 2
    assert finalized.status == "partial" and finalized.pending_after == 2

    respx.reset()
    serve({f"p{i}": ("2026-01-01", f"Page {i}") for i in range(5)})
    prepared2, compiled2, finalized2, _ = cycle(tmp_path, FakeQmd(existing={"kb-example-com"}), budget=3)
    assert len(compiled2) == 2 and finalized2.status == "ok"
    state = load_state(config.WorkspacePaths(tmp_path).site("example-com").state_file)
    assert all(p.status == PageStatus.COMPILED for p in state.pages.values())


@respx.mock
def test_cli_sitemap_failure_aborts_cleanly(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    respx.get(SITEMAP).mock(return_value=httpx.Response(503))
    runner = CliRunner()
    out = runner.invoke(app, ["--root", str(tmp_path), "sync", "prepare", "example-com", "--time-budget", "60"])
    result = json.loads(out.output)
    assert result["status"] == "failed"
    # The skill always finishes with qmd refresh, which releases the lock when nothing is dirty
    done = json.loads(runner.invoke(app, ["--root", str(tmp_path), "qmd", "refresh", "--run-id", result["run_id"]]).output)
    assert done["status"] == "skipped"
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_cli_batches_requires_queue(tmp_path: Path) -> None:
    config.add_site(tmp_path, SITEMAP)
    out = CliRunner().invoke(app, ["--root", str(tmp_path), "sync", "batches", "example-com", "--run-id", "nope"])
    assert out.exit_code == 2 and "prepare" in out.output
