# Knowledge Base Builder

A Claude Code plugin that turns websites into a structured markdown wiki and keeps it current. Point it at a sitemap once; after that it fetches only new or changed pages, recompiles just those articles, archives pages that disappear, and refreshes a [qmd](https://github.com/tobi/qmd) search collection so everything is searchable by keyword and meaning.

```
sitemap.xml ──► diff vs state ──► fetch changed pages ──► raw markdown
                                                              │
qmd search ◄── qmd update + embed ◄── wiki + indexes ◄── Haiku compile agents
```

## What a sync does

| Step | What happens | LLM cost |
|------|--------------|----------|
| Discover | Reads the sitemap (nested indexes and `.xml.gz` included). Any read failure stops the run before anything changes. | none |
| Diff | Classifies every URL as new, updated, unchanged, removed or reappeared, using `<lastmod>` when the site provides it. | none |
| Fetch | Fetches only what the diff needs (conditional GET), converts to markdown, and compares a content hash. A lastmod bump with identical content is not recompiled. | none |
| Compile | Parallel Haiku sub-agents write articles for queued pages into a staging folder. | ~$0.004/page on API billing; included in a Claude subscription |
| Finalize | Publishes staged articles into `wiki/<topic>/`, moves reclassified articles, archives removed pages, restores reappearing ones, rebuilds indexes, writes a run report. | none |
| Index | Creates the site's qmd collection on first use, then `qmd update` + `qmd embed`. | none (local models) |

## Install

Prerequisites: Claude Code (desktop app for scheduled syncs), [uv](https://docs.astral.sh/uv/), Python 3.12+, and qmd (`npm install -g @tobilu/qmd`).

In Claude Code:

```
/plugin marketplace add promptgtm-shared/knowledge-base-builder
/plugin install kb-builder@kb-builder
```

The plugin's `kb-builder` command creates its Python environment on first use.

## Usage

| Command | Purpose |
|---------|---------|
| `/kb-builder:add-site https://example.com/sitemap.xml` | Pick the workspace folder (first time), register a site, review its topics, run the first sync, offer scheduling |
| `/kb-builder:sync <site-id\|all>` | Sync now. Flags: `--full` (re-check every page's content), `--approve-archive`, `--budget N` |
| `/kb-builder:schedule <site-id>` | Sync automatically via a desktop scheduled task (`... off` to stop) |
| `/kb-builder:status` | Sites, queues, held archives, pending indexing, last run report |
| `/kb-builder:ingest` | Compile your own `.md`, `.txt`, `.docx`, `.pdf` files from `<workspace>/input/` |

Search the result with qmd, e.g. `qmd search "pricing tiers" -c kb-example-com`.

## Workspace layout

All data lives in the workspace folder you choose during `/kb-builder:add-site` (saved by `kb-builder workspace set`), never inside the plugin:

```
<workspace>/
  sites.json                 # tracked sites + unattended_cap
  input/                     # local documents for /kb-builder:ingest
  sites/<site-id>/
    wiki/                    # qmd collection kb-<site-id> points here
      _master-index.md
      <topic>/_index.md
      <topic>/<slug>.md
    archive/<topic>/<slug>.md
    raw/<slug>.md
    staging/<run-id>/        # compile agent output for the current run
    runs/<timestamp>.md      # run reports
    state.json               # per-page sync state
```

Each article starts with its title followed by `**Source:**` (page URL), `**Created:**`, `**Updated:**` (after a recompile) and `**Topics:**`, then Summary, Key Takeaways, Details and Related sections.

## Sync behaviour worth knowing

- **Removed pages are archived, not deleted.** If a page comes back with the same content it is restored without a recompile.
- **Mass-archive guard.** When a site has 10+ known pages and more than 20% vanish from the sitemap in one run, nothing is archived and the run report and `status` say so. Check the site, then run `/kb-builder:sync <site-id> --approve-archive`.
- **Compile cap.** Unattended runs compile at most `unattended_cap` pages in total (default 50, in `sites.json`). The rest stay queued, oldest first, and are picked up by the next run. Pages whose content changes on every visit are flagged as churning and go last.
- **Sites without `<lastmod>`.** Every page is re-fetched each run (cheap) and only compiled when its content hash changes. `--full` forces this for sites whose lastmod you don't trust.
- **One sync at a time.** A workspace lock is shared by every step of a run; a run that finds it held reports `skipped`. A crashed run's lock expires after 6 hours.
- **qmd refresh is global.** qmd cannot reindex a single collection, so `qmd update` reindexes all your collections and runs their `update:` hooks. It runs once per sync and only when a wiki changed. Set `KB_QMD_INDEX=<name>` to keep kb collections in a separate named qmd index instead (the default `qmd search` and qmd MCP server then won't see them).
- **Your own files are safe.** Notes you add under a site's wiki are listed as untracked in run reports, never deleted. Manual edits to compiled articles are overwritten when that page is recompiled.

## Scheduling

`/kb-builder:schedule` creates one desktop scheduled task, `kb-builder-sync`, running `/kb-builder:sync all --unattended` for every site flagged as scheduled. Runs happen while the Claude desktop app is open; a missed run catches up on the next launch. Click **Run now** once and approve the tools it asks for so later runs never wait on a prompt.

Headless alternative (no desktop app; requires a valid `claude auth login`):

```bash
claude -p "/kb-builder:sync all --unattended" --add-dir "<workspace>" --permission-mode dontAsk --allowedTools "Bash(kb-builder:*)" "Skill" "Agent" "Read" "Write" "AskUserQuestion"
```

Run it from Windows Task Scheduler or cron with the workspace as working directory and an absolute path to `claude`. Don't use `--bare`; it skips installed plugins.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `No workspace configured` | Run `/kb-builder:add-site` (it asks for the folder) |
| `qmd was not found on PATH` | Install qmd; the wiki is still updated and indexing is retried next sync |
| `skipped: Workspace is locked` | Another sync is running; wait, or it expires 6 hours after its last activity |
| `HELD ARCHIVE` in status | Review the listed URLs, then sync with `--approve-archive` |
| Sitemap read failed | Nothing changed; check the sitemap URL shown by `kb-builder site show <id>` |

## Development

```bash
uv sync
uv run pytest
uv run kb --help
```

The CLI (`src/kb/`) holds all deterministic logic: sitemap discovery, diffing, fetching, state, locking, finalize, indexes and qmd calls. Skills in `skills/` orchestrate it and run the LLM compile step. `bin/kb-builder` is the wrapper skills call; the plugin's `bin/` is on PATH inside Claude Code.

## License

MIT
