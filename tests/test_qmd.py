from pathlib import Path

import pytest

from kb import config, locks
from kb.qmd import Qmd, QmdError, refresh, safe_text, site_contexts, subprocess_runner
from kb.state import load_state, save_state

DONE = "✓ Done! Embedded 4 chunks from 2 documents in 3s"
CLEAN = "✓ All content hashes already have embeddings."


class FakeQmd:
    """Scripted stand-in for the qmd binary: records calls, answers by subcommand."""

    def __init__(self, existing: set[str] | None = None, embed_outputs: list[str] | None = None, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.existing = set(existing or ())
        self.embed_outputs = list(embed_outputs or [CLEAN])
        self.fail_on = fail_on

    def __call__(self, args: list[str], timeout: float) -> tuple[int, str]:
        self.calls.append(args)
        if self.fail_on and args[0] == self.fail_on:
            return 1, "boom: model unavailable"
        if args[:2] == ["collection", "show"]:
            return (0, f"Collection: {args[2]}") if args[2] in self.existing else (1, "Collection not found")
        if args[:2] == ["collection", "add"]:
            self.existing.add(args[args.index("--name") + 1])
            return 0, "created"
        if args[0] == "embed":
            return 0, self.embed_outputs.pop(0) if self.embed_outputs else CLEAN
        return 0, ""

    def commands(self) -> list[str]:
        return [" ".join(c[:2]) if c[0] in ("collection", "context") else c[0] for c in self.calls]


def setup(root: Path, dirty: dict[str, bool]) -> str:
    for site_id, is_dirty in dirty.items():
        config.add_site(root, f"https://{site_id}.example/sitemap.xml", site_id=site_id)
        paths = config.WorkspacePaths(root).site(site_id)
        state = load_state(paths.state_file)
        state.qmd_dirty = is_dirty
        save_state(paths.state_file, state)
    return locks.acquire(config.WorkspacePaths(root).lock_file)


def site_state(root: Path, site_id: str):
    return load_state(config.WorkspacePaths(root).site(site_id).state_file)


def test_new_collection_created_with_contexts_then_update_and_embed(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    fake = FakeQmd(embed_outputs=[DONE, CLEAN])
    result = refresh(tmp_path, run_id, Qmd(fake))
    assert result.status == "done" and result.created_collections == ["kb-alpha"] and result.updated
    cmds = fake.commands()
    assert cmds[:2] == ["collection show", "collection add"]
    assert cmds.count("update") == 1 and cmds.index("update") > cmds.index("collection add")
    contexts = [c for c in fake.calls if c[0] == "context"]
    assert contexts[0][:3] == ["context", "add", "qmd://kb-alpha/"]
    assert any(c[2] == "qmd://kb-alpha/general" for c in contexts)
    assert fake.calls[-1][:3] == ["embed", "-c", "kb-alpha"]
    assert not site_state(tmp_path, "alpha").qmd_dirty
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_existing_collection_only_updates_and_embeds_and_contexts_not_repeated(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    refresh(tmp_path, run_id, Qmd(FakeQmd(existing={"kb-alpha"})))
    paths = config.WorkspacePaths(tmp_path).site("alpha")
    state = load_state(paths.state_file)
    state.qmd_dirty = True
    save_state(paths.state_file, state)
    run2 = locks.acquire(config.WorkspacePaths(tmp_path).lock_file)
    fake = FakeQmd(existing={"kb-alpha"})
    refresh(tmp_path, run2, Qmd(fake))
    assert fake.commands() == ["collection show", "update", "embed"]


def test_two_dirty_sites_update_once(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True, "beta": True, "gamma": False})
    fake = FakeQmd(existing={"kb-alpha", "kb-beta"})
    result = refresh(tmp_path, run_id, Qmd(fake))
    assert fake.commands().count("update") == 1
    assert [c[2] for c in fake.calls if c[0] == "embed"] == ["kb-alpha", "kb-beta"]
    assert sorted(result.embedded) == ["alpha", "beta"]


def test_nothing_dirty_runs_no_qmd_command(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": False})
    fake = FakeQmd()
    result = refresh(tmp_path, run_id, Qmd(fake))
    assert result.status == "skipped" and fake.calls == []
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_embed_failures_keep_site_dirty(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    fake = FakeQmd(existing={"kb-alpha"}, embed_outputs=[DONE + "\n⚠ 3 chunks still failed after retries"])
    result = refresh(tmp_path, run_id, Qmd(fake))
    assert result.status == "failed" and result.still_dirty == ["alpha"]
    assert site_state(tmp_path, "alpha").qmd_dirty


def test_embed_error_exit_keeps_dirty_and_releases_lock(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    result = refresh(tmp_path, run_id, Qmd(FakeQmd(existing={"kb-alpha"}, fail_on="embed")))
    assert result.status == "failed" and "boom" in result.message
    assert site_state(tmp_path, "alpha").qmd_dirty
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file) is None


def test_time_budget_exhausted_returns_incomplete_and_continuation_skips_update(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    result = refresh(tmp_path, run_id, Qmd(FakeQmd(existing={"kb-alpha"})), time_budget_s=10)
    assert result.status == "incomplete" and result.updated
    assert locks.read_lock(config.WorkspacePaths(tmp_path).lock_file).run_id == run_id

    fake = FakeQmd(existing={"kb-alpha"})
    second = refresh(tmp_path, run_id, Qmd(fake))
    assert second.status == "done"
    assert "update" not in fake.commands()


def test_retry_on_next_run_when_still_dirty(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    refresh(tmp_path, run_id, Qmd(FakeQmd(existing={"kb-alpha"}, fail_on="update")))
    assert site_state(tmp_path, "alpha").qmd_dirty
    run2 = locks.acquire(config.WorkspacePaths(tmp_path).lock_file)
    fake = FakeQmd(existing={"kb-alpha"})
    assert refresh(tmp_path, run2, Qmd(fake)).status == "done"
    assert "update" in fake.commands()


def test_missing_binary_is_actionable() -> None:
    runner = subprocess_runner(binary=None)
    import kb.qmd as qmd_module

    original = qmd_module.shutil.which
    qmd_module.shutil.which = lambda _: None
    try:
        with pytest.raises(QmdError, match="not found on PATH"):
            subprocess_runner()(["status"], 5)
    finally:
        qmd_module.shutil.which = original
    assert callable(runner)


def test_remove_collection(tmp_path: Path) -> None:
    fake = FakeQmd(existing={"kb-alpha"})
    assert Qmd(fake).remove_collection("kb-alpha")
    assert not Qmd(FakeQmd()).remove_collection("kb-missing")


def test_context_text_safe_for_windows_shim() -> None:
    assert safe_text('Product & Features | "quoted" <x> 100%') == "Product and Features  quoted x 100"


def test_site_contexts_wording(tmp_path: Path) -> None:
    site = config.add_site(tmp_path, "https://example.com/sitemap.xml", description="Example docs")
    contexts = site_contexts(site)
    assert contexts["qmd://kb-example-com/"] == "Example docs. Start at _master-index.md."
    assert contexts["qmd://kb-example-com/general"] == "General articles."


def test_contexts_sent_sanitized(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    fake = FakeQmd()
    refresh(tmp_path, run_id, Qmd(fake))
    sent = [c[3] for c in fake.calls if c[0] == "context"]
    assert "Product and Features articles." in sent and not any("&" in s for s in sent)


def test_new_topic_adds_only_its_context(tmp_path: Path) -> None:
    run_id = setup(tmp_path, {"alpha": True})
    refresh(tmp_path, run_id, Qmd(FakeQmd()))
    ws = config.load_workspace(tmp_path)
    ws.sites["alpha"].taxonomy["pricing"] = "Pricing"
    config.save_workspace(tmp_path, ws)
    paths = config.WorkspacePaths(tmp_path).site("alpha")
    state = load_state(paths.state_file)
    state.qmd_dirty = True
    save_state(paths.state_file, state)

    fake = FakeQmd(existing={"kb-alpha"})
    refresh(tmp_path, locks.acquire(config.WorkspacePaths(tmp_path).lock_file), Qmd(fake))
    assert [c[2] for c in fake.calls if c[0] == "context"] == ["qmd://kb-alpha/pricing"]


def test_embed_needs_a_minute_of_grace(tmp_path: Path) -> None:
    import time

    fake = FakeQmd()
    assert Qmd(fake).embed("kb-x", time.monotonic() + 100) == "timeout"
    assert fake.calls == []
    assert Qmd(fake).embed("kb-x", time.monotonic() + 200) == "done"
    assert fake.calls[0][-1] == "2"
