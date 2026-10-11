"""qmd integration: one collection per site, refreshed once per run when a wiki changed.

`qmd update` has no collection filter: it reindexes every collection and runs each
collection's `update:` hook. It is therefore called at most once per run and only
when at least one site's wiki changed. `qmd embed -c` is scoped per collection.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from kb import config, locks
from kb.state import load_state, save_state

COLLECTION_PREFIX = "kb-"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
NOTHING_PENDING = ("All content hashes already have embeddings", "No non-empty documents to embed")
EMBED_FAILURES = "still failed after retries"
MAX_EMBED_PASSES = 3
INSTALL_HINT = "Install it with `npm install -g @tobilu/qmd` (or bun), then rerun `kb qmd refresh`."


EmbedOutcome = Literal["done", "timeout", "failed"]


class QmdError(Exception):
    pass


class Runner(Protocol):
    def __call__(self, args: list[str], timeout: float) -> tuple[int, str]: ...


def subprocess_runner(binary: str | None = None) -> Runner:
    resolved = binary or shutil.which("qmd")
    # Optional named qmd index (e.g. to keep kb collections out of the default index)
    index = os.environ.get("KB_QMD_INDEX")
    prefix = ["--index", index] if index else []

    def run(args: list[str], timeout: float) -> tuple[int, str]:
        if not resolved:
            raise QmdError(f"`qmd` was not found on PATH. {INSTALL_HINT}")
        env = {**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}
        try:
            proc = subprocess.run(
                [resolved, *prefix, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise QmdError(f"`qmd {' '.join(args)}` timed out after {int(timeout)}s") from e
        return proc.returncode, ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))

    return run


def safe_text(text: str) -> str:
    """qmd.exe on Windows re-parses arguments through cmd.exe, so shell metacharacters split the command."""
    text = text.replace("&", "and")
    return re.sub(r'[|<>^%"!]', "", text).strip()


def collection_name(site_id: str) -> str:
    return f"{COLLECTION_PREFIX}{site_id}"


def site_contexts(site: config.Site) -> dict[str, str]:
    name = collection_name(site.id)
    root_text = site.description or (
        f"Knowledge base compiled from {site.sitemap_url}." if site.sitemap_url else "Knowledge base compiled from local files."
    )
    contexts = {f"qmd://{name}/": f"{root_text.rstrip('.')}. Start at _master-index.md."}
    for topic, label in site.taxonomy.items():
        contexts[f"qmd://{name}/{topic}"] = f"{label} articles."
    return contexts


class Qmd:
    def __init__(self, runner: Runner | None = None) -> None:
        self.run = runner or subprocess_runner()

    def _check(self, args: list[str], timeout: float = 300) -> str:
        code, out = self.run(args, timeout)
        if code != 0:
            tail = "\n".join(out.strip().splitlines()[-5:])
            raise QmdError(f"`qmd {' '.join(args)}` failed (exit {code}): {tail}")
        return out

    def collection_exists(self, name: str) -> bool:
        code, _ = self.run(["collection", "show", name], 60)
        return code == 0

    def ensure_collection(self, name: str, wiki: Path) -> bool:
        """Create the collection if missing (qmd indexes it on creation). Returns True if created."""
        if self.collection_exists(name):
            return False
        wiki.mkdir(parents=True, exist_ok=True)
        self._check(["collection", "add", str(wiki), "--name", name], timeout=1800)
        return True

    def add_context(self, target: str, text: str) -> None:
        self._check(["context", "add", target, safe_text(text)], timeout=60)

    def update(self, timeout: float) -> None:
        self._check(["update"], timeout=timeout)

    def embed(self, name: str, deadline: float) -> EmbedOutcome:
        """Embed until qmd reports nothing pending for the collection."""
        for _ in range(MAX_EMBED_PASSES):
            remaining = deadline - time.monotonic()
            if remaining < 30:
                return "timeout"
            minutes = max(int(remaining // 60) - 1, 1)
            out = self._check(["embed", "-c", name, "--timeout", str(minutes)], timeout=remaining)
            if any(marker in out for marker in NOTHING_PENDING):
                return "done"
            if EMBED_FAILURES in out:
                return "failed"
        return "timeout"

    def remove_collection(self, name: str) -> bool:
        if not self.collection_exists(name):
            return False
        self._check(["collection", "remove", name], timeout=600)
        return True


class RefreshResult(BaseModel):
    status: Literal["done", "incomplete", "failed", "skipped"]
    run_id: str
    updated: bool = False
    embedded: list[str] = Field(default_factory=list)
    still_dirty: list[str] = Field(default_factory=list)
    created_collections: list[str] = Field(default_factory=list)
    message: str = ""


def refresh(
    root: Path,
    run_id: str,
    qmd: Qmd | None = None,
    time_budget_s: float = 480.0,
) -> RefreshResult:
    """Refresh qmd for every dirty site, then release the run lock when nothing is left to do."""
    ws_paths = config.WorkspacePaths(root)
    lock = locks.verify(ws_paths.lock_file, run_id)
    deadline = time.monotonic() + time_budget_s
    ws = config.load_workspace(root)
    states = {sid: load_state(ws_paths.site(sid).state_file) for sid in ws.sites}
    dirty = [sid for sid, st in states.items() if st.qmd_dirty]
    result = RefreshResult(status="done", run_id=run_id)

    if not dirty:
        locks.release(ws_paths.lock_file, run_id)
        result.status = "skipped"
        result.message = "No wiki changes; qmd untouched."
        return result

    qmd = qmd or Qmd()
    try:
        for sid in dirty:
            site, state, paths = ws.sites[sid], states[sid], ws_paths.site(sid)
            name = collection_name(sid)
            if qmd.ensure_collection(name, paths.wiki):
                result.created_collections.append(name)
            for target, text in site_contexts(site).items():
                if target not in state.qmd_contexts:
                    qmd.add_context(target, text)
                    state.qmd_contexts.append(target)
            save_state(paths.state_file, state)

        # Global reindex once per run; a continuation of the same run skips it
        if not lock.qmd_updated:
            qmd.update(timeout=max(deadline - time.monotonic(), 60))
            locks.verify(ws_paths.lock_file, run_id, qmd_updated=True)
            result.updated = True

        outcomes: dict[str, EmbedOutcome] = {}
        for sid in dirty:
            outcomes[sid] = qmd.embed(collection_name(sid), deadline)
            if outcomes[sid] == "done":
                states[sid].qmd_dirty = False
                save_state(ws_paths.site(sid).state_file, states[sid])
                result.embedded.append(sid)
            else:
                result.still_dirty.append(sid)
    except QmdError as e:
        result.status = "failed"
        result.message = f"{e} Wiki changes are saved; qmd will be retried on the next run."
        result.still_dirty = [sid for sid in dirty if states[sid].qmd_dirty]
        locks.release(ws_paths.lock_file, run_id)
        return result

    if "timeout" in outcomes.values():
        result.status = "incomplete"
        result.message = "Embedding not finished within the time budget; call `kb qmd refresh --run-id` again."
        return result
    if result.still_dirty:
        result.status = "failed"
        result.message = "Some chunks failed to embed; those sites stay marked for retry on the next run."
    locks.release(ws_paths.lock_file, run_id)
    return result
