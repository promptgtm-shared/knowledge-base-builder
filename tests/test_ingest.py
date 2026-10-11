import json
from pathlib import Path

from typer.testing import CliRunner

from kb import config, locks, sync
from kb.cli import app
from kb.finalize import finalize
from kb.state import PageStatus, load_state

ARTICLE = "# {title}\n\n## Summary\n\n" + "Summary sentence about the ingested document. " * 6 + "\n"
FIXTURE_DOCX = Path(__file__).parent / "fixtures" / "docs" / "guide.docx"


def compile_and_finalize(root: Path, run_id: str) -> dict:
    plan = sync.plan_batches(root, "local", run_id)
    for batch in plan.batches:
        for item in batch.items:
            Path(item.output_path).write_text(ARTICLE.format(title=item.slug), encoding="utf-8")
        Path(batch.manifest).write_text(json.dumps([{"slug": i.slug, "topic": "general"} for i in batch.items]), encoding="utf-8")
    result = finalize(root, "local", run_id).model_dump()
    locks.release(config.WorkspacePaths(root).lock_file, run_id)
    return result


def test_ingest_new_unchanged_and_modified(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "guide.docx").write_bytes(FIXTURE_DOCX.read_bytes())
    (input_dir / "notes.md").write_text("# Notes\n\n" + "Meeting notes content. " * 10, encoding="utf-8")
    (input_dir / "image.png").write_bytes(b"\x89PNG")

    first = sync.prepare_local(tmp_path)
    assert first.queue_size == 2 and first.fetched == {"changed": 2, "unsupported": 1}
    done = compile_and_finalize(tmp_path, first.run_id)
    assert done["compiled"] == 2

    wiki_file = config.WorkspacePaths(tmp_path).site("local").wiki / "general" / "guide.md"
    assert "**Source:** local://guide.docx" in wiki_file.read_text(encoding="utf-8")
    assert (input_dir / "guide.docx").exists()  # inputs are left in place

    second = sync.prepare_local(tmp_path)
    assert second.queue_size == 0 and second.fetched["unchanged"] == 2
    locks.release(config.WorkspacePaths(tmp_path).lock_file, second.run_id)

    (input_dir / "notes.md").write_text("# Notes v2\n\n" + "Updated meeting notes. " * 10, encoding="utf-8")
    third = sync.prepare_local(tmp_path)
    assert third.queue_size == 1
    compile_and_finalize(tmp_path, third.run_id)
    state = load_state(config.WorkspacePaths(tmp_path).site("local").state_file)
    assert state.pages["local://notes.md"].status == PageStatus.COMPILED
    assert len(list((config.WorkspacePaths(tmp_path).site("local").wiki / "general").glob("notes*.md"))) == 1


def test_removed_input_file_is_not_archived(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.md").write_text("# A\n\n" + "content " * 30, encoding="utf-8")
    run = sync.prepare_local(tmp_path)
    compile_and_finalize(tmp_path, run.run_id)
    (input_dir / "a.md").unlink()

    run2 = sync.prepare_local(tmp_path)
    result = compile_and_finalize(tmp_path, run2.run_id)
    assert result["archived"] == 0
    assert (config.WorkspacePaths(tmp_path).site("local").wiki / "general" / "a.md").exists()


def test_cli_ingest_and_status(tmp_path: Path) -> None:
    runner = CliRunner()
    base = ["--root", str(tmp_path)]
    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "a.md").write_text("# A\n\n" + "content " * 30, encoding="utf-8")

    out = runner.invoke(app, [*base, "ingest"])
    result = json.loads(out.output)
    assert result["status"] == "ready" and result["queue_size"] == 1

    text = runner.invoke(app, [*base, "status"]).output
    assert "Sync in progress" in text and "local - local files" in text and "queued for compile: 1" in text

    status = json.loads(runner.invoke(app, [*base, "status", "--json"]).output)
    assert {s["id"] for s in status["sites"]} == {"example-com", "local"}
    assert status["lock"]["run_id"] == result["run_id"] and status["unattended_cap"] == 50

    sync.abort(tmp_path, result["run_id"])
    assert "Sync in progress" not in runner.invoke(app, [*base, "status"]).output


def test_status_shows_held_archive_and_last_run(tmp_path: Path) -> None:
    from kb.state import save_state

    config.add_site(tmp_path, "https://example.com/sitemap.xml")
    paths = config.WorkspacePaths(tmp_path).site("example-com")
    state = load_state(paths.state_file)
    state.held_archive = ["https://example.com/a", "https://example.com/b"]
    state.last_run = {"status": "partial", "report": "runs/x.md"}
    state.qmd_dirty = True
    save_state(paths.state_file, state)
    text = CliRunner().invoke(app, ["--root", str(tmp_path), "status"]).output
    assert "HELD ARCHIVE: 2 pages" in text and "--approve-archive" in text and "https://example.com/b" in text
    assert "last run: partial - runs/x.md" in text and "qmd pending" in text
