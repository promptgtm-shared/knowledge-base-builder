---
name: scrape-and-compile
description: End-to-end pipeline — scrape a website from its sitemap, ingest any files in input/, then compile everything into the wiki. Invoke with /scrape-and-compile <sitemap-url> or /scrape-and-compile (to just ingest + compile).
allowed-tools: Agent, Bash, Read, Write, Edit, Glob, Grep, Skill, TodoWrite
---

# Scrape and Compile Pipeline

## What This Skill Does

Chains the full knowledge base pipeline in one command:
1. Scrape website pages from a sitemap URL (optional)
2. Ingest files from `input/` folder (docx, pdf, txt → markdown)
3. Compile all new raw markdown into structured wiki articles
4. Build/update wiki indexes

## Cost Warning

**IMPORTANT:** Before starting any pipeline mode, warn the user:

> This pipeline will spawn parallel sub-agents for compilation.
> - Scraping + ingesting: no LLM cost (pure Python).
> - Compiling: uses Haiku sub-agents. Claude Code subscription = no extra cost. Anthropic API = ~$0.004/file.
>
> Proceed? (yes/no)

Wait for explicit confirmation before proceeding. Do NOT skip this step.

## Instructions

### Mode 1: Full scrape + compile

When invoked as `/scrape-and-compile <sitemap-url>`:

**Step 1: Scrape**
```bash
uv run python scrape.py <sitemap-url>
```
This outputs markdown files to `raw/<domain>/`. Wait for completion. Report the number of pages scraped.

If the scrape fails, stop and report the error. Do not proceed to compile.

**Step 2: Ingest input/ folder**
Check if `input/` directory exists and has files:
```bash
ls input/ 2>/dev/null | head -5
```
If files exist, run:
```bash
uv run python ingest.py
```
This converts docx/pdf to markdown and moves md/txt files to `raw/`. Wait for completion.

**Step 3: Compile**
Invoke the compile-wiki skill on the scraped directory:
```
/compile-wiki raw/<domain>/
```

If there are also ingested files in `raw/` from other sources (not the domain subfolder), compile those too.

**Step 4: Report**
```
Pipeline complete!
- Pages scraped: N
- Files ingested: M
- Wiki articles created: K
- Total wiki articles: T
```

### Mode 2: Ingest + compile only

When invoked as `/scrape-and-compile` (no URL):

Skip Step 1. Run Steps 2-4 only:
- Ingest any files in `input/`
- Compile any new files in `raw/` that aren't in the compiled log

### Mode 3: Compile only

When invoked as `/scrape-and-compile --compile-only`:

Skip Steps 1-2. Run Steps 3-4 only:
- Compile any new files in `raw/` not yet in the compiled log

## Error Handling

- Scrape failure → stop before compile, report error
- Ingest failure on individual file → skip that file, continue with others
- Compile failure → handled by compile-wiki skill (per-batch error isolation)

## Cloud Deployment Note

This same pipeline runs as an Anthropic remote trigger in production.
The only difference: cloud agents commit and push wiki changes back to the repo.
Local agents write to the filesystem directly.
