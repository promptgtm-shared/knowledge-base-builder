---
name: status
description: Show the state of the knowledge base — tracked sites, last sync, pages queued for compile, held archives, pending qmd indexing, and the latest run report. Use when the user asks what is tracked, whether syncs are working, or why a page is missing.
allowed-tools: Bash(kb-builder:*), Read
---

# Knowledge base status

Run `kb-builder status` (or `kb-builder status --json` when you need exact values: `lock`, `unattended_cap`, and per site `archive_held` URLs, `queued`, `qmd_dirty`, `last_run`). If it fails with "No workspace configured", tell the user to start with `/kb-builder:add-site`.

Present the result per site, leading with anything that needs attention:

- **HELD ARCHIVE**: many pages vanished from the sitemap at once, so nothing was archived. Suggest checking the site, then `/kb-builder:sync <site-id> --approve-archive`.
- **qmd pending**: the wiki changed but search indexing has not finished; the next sync retries it, or run `/kb-builder:sync <site-id>` now.
- **queued for compile**: pages fetched but not yet compiled (usually the per-run cap). Mention churning pages if shown: their content changes on every visit, so they keep coming back.
- **last run** `failed` or `partial`: offer to open the report file it names and summarize it.
- A "Sync in progress" line: another run holds the workspace; it releases itself when done. A "Stale lock" line means a run crashed; the next sync reclaims it, or `kb-builder sync unlock` removes it now.

## Other changes the user may ask for

- **Compile cap for scheduled runs:** `kb-builder workspace cap` shows it, `kb-builder workspace cap <N>` sets it.
- **Stop tracking a site:** confirm with the user first. `kb-builder site remove <site-id>` keeps its files; `kb-builder site remove <site-id> --purge` also deletes its folder and qmd collection and cannot be undone, so only use `--purge` when the user explicitly asks to delete the data.
- **Pause scheduling for a site:** `/kb-builder:schedule <site-id> off`.

If the user asks about one page or failure, read the latest run report for that site (path in `last run`).
