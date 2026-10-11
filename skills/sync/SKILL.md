---
name: sync
description: Sync tracked websites into the knowledge base — read each sitemap, fetch only new or changed pages, compile them into wiki articles, archive removed pages, and refresh the qmd search index. Use when the user asks to sync, update, refresh or check a tracked site for new posts/pages, or when a scheduled task runs `/kb-builder:sync all --unattended`.
argument-hint: "<site-id|all> [--unattended] [--full] [--approve-archive] [--budget N]"
allowed-tools: Bash(kb-builder:*), Skill, Agent, Read, AskUserQuestion
---

# Sync tracked sites

Arguments: `$ARGUMENTS`

- First word: a site id, or `all`.
- `--unattended`: a scheduled run. Never ask questions; apply the workspace compile cap; ignore `--approve-archive`.
- `--full`: re-check every page's content even if its sitemap lastmod did not change.
- `--approve-archive`: release a held mass-archive (interactive only).
- `--budget N`: compile at most N pages now; the rest stay queued for later runs.

Every `kb-builder` command below prints JSON (except `abort`). Read the fields named below; do not guess. If `kb-builder` itself is missing or fails with "No workspace configured", stop and tell the user to run `/kb-builder:add-site` first.

## 1. Pick the sites

- A specific site id: just that site. If `prepare` reports it unknown, show `kb-builder site list` and stop.
- `all` interactive: the `id` of every entry in `kb-builder site list --json --sitemap-only`.
- `all --unattended`: the `id` of every entry in `kb-builder site list --json --sitemap-only --scheduled`. If the list is empty, report that no site is scheduled and stop.

## 2. Prepare, compile and finalize each site

Keep `RUN_ID` (empty at first) and `QUEUED` (0 at first). For each site in order:

1. Run `kb-builder sync prepare <site> [--run-id RUN_ID] [--full] [--approve-archive] [--budget N] [--unattended --already-queued QUEUED]`.
   - Exit code 3 with `"status": "skipped"`: another sync holds the workspace. Report it and stop the whole run; do not abort or release anything.
   - Save `run_id` from the first result as `RUN_ID` and pass `--run-id RUN_ID` on every later call.
   - `"status": "incomplete"`: run the same command again (with `--run-id`) until it is no longer incomplete.
   - `"status": "failed"`: note the `message` for the summary and continue with the next site. Nothing was changed for that site.
   - `"status": "ready"`: continue below.
2. If `archive_held` is non-empty, include its URLs in the summary with the note that a large share of known pages vanished from the sitemap at once, so archiving is on hold. Interactive runs: tell the user they can rerun `/kb-builder:sync <site> --approve-archive` after checking the site.
3. If `queue_size` > 0:
   - Interactive: tell the user how many pages will be compiled (`queue_size`, plus `deferred` if any) and the `estimated_cost_usd` (Haiku sub-agents; included in a Claude subscription, about that much on API billing). Ask with AskUserQuestion whether to compile now or abort. On abort, run `kb-builder sync abort --run-id RUN_ID` and stop.
   - Invoke the Skill tool with skill `kb-builder:compile-wiki` and args `<site> --run-id RUN_ID`.
4. Run `kb-builder sync finalize <site> --run-id RUN_ID` (also when the queue was empty: it archives, restores and rebuilds indexes). Keep its JSON for the summary.
5. Add this site's `queue_size` to `QUEUED`.

## 3. Refresh search and release the lock

If `RUN_ID` is set, run `kb-builder qmd refresh --run-id RUN_ID`, repeating while `"status": "incomplete"`. This always runs last because it releases the workspace lock. `"status": "failed"` is not fatal: the wiki is saved and qmd is retried on the next sync; include the `message` in the summary.

## On any unexpected error

If a command fails in a way not covered above, or you must stop early, run `kb-builder sync abort --run-id RUN_ID` (when `RUN_ID` is set) before reporting, so the workspace is not left locked.

## 4. Summary

One short block per site: pages new/changed/removed (from `diff`), articles compiled, compile failures, archived, restored, still queued (`pending_after`), churning pages if any, held archive if any, and the run report path (`report`). Then the qmd result. For unattended runs this summary is the final message, so make problems obvious: start with `PROBLEM:` lines for failed sites, held archives, qmd failures, or compile failures.
