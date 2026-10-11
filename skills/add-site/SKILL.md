---
name: add-site
description: Start tracking a website in the knowledge base — choose the workspace folder on first use, register the site's sitemap, review its topic taxonomy, run the first sync, and offer to schedule recurring syncs. Use when the user wants to add, track, watch or build a knowledge base from a website or sitemap URL.
argument-hint: "<sitemap-url> [--id <site-id>]"
allowed-tools: Bash(kb-builder:*), Skill, Read, Write, AskUserQuestion
---

# Add a site

Arguments: `$ARGUMENTS` — a sitemap URL (for example `https://example.com/sitemap.xml`) and optionally `--id <site-id>`. If no URL was given, ask for it. If the user gave a homepage instead of a sitemap, try `<homepage>/sitemap.xml`, and tell them which URL you used.

## 1. Workspace

Run `kb-builder workspace show`. If it fails with "No workspace configured", ask the user where the knowledge base should live (suggest `~/kb`), then run `kb-builder workspace set "<folder>"`. Every tracked site, its wiki and its sync state go in that folder.

## 2. Register

Ask for a one-line description of the site (optional; it becomes the qmd search context). Then run:

```bash
kb-builder site add "<sitemap-url>" [--id <site-id>] --description "<description>"
```

If it reports the site or sitemap already exists, stop and offer `/kb-builder:sync <site-id>` instead.

## 3. Taxonomy

Run `kb-builder site show <site-id>` and show the user the `taxonomy` (topic folder → label). Articles are filed into these folders. Ask whether to keep it or adjust it. To change it, write the full JSON object (folder → label, lowercase-hyphenated folder names) to a temporary file and run `kb-builder site update <site-id> --taxonomy-file "<file>"`. `general` is always kept as the fallback.

## 4. First sync

Ask how much to compile in the first sync:

- **Everything now**: invoke the Skill tool with skill `kb-builder:sync` and args `<site-id>`.
- **A first batch now, the rest on schedule**: ask for N (suggest 50), then invoke `kb-builder:sync` with args `<site-id> --budget N`. The remaining pages stay queued, and scheduled runs work through them at the workspace cap per run.

The sync skill shows the page count and estimated cost before compiling.

## 5. Schedule

When the first sync finished preparing (the site now has `first-sync-done` in `kb-builder site list`), ask whether to sync this site on a schedule. If yes, invoke the Skill tool with skill `kb-builder:schedule` and args `<site-id>`.

Finish with where the wiki lives (`<workspace>/sites/<site-id>/wiki/`) and the qmd collection name `kb-<site-id>`, for example `qmd search "<terms>" -c kb-<site-id>`.
