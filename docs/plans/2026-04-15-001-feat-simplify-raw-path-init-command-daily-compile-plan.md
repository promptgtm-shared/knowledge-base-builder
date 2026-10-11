---
title: "Simplify raw/ path, add /init command, daily compile schedule"
type: feat
status: superseded
date: 2026-04-15
superseded_by: docs/plans/2026-10-11-001-feat-sitemap-sync-qmd-plugin-plan.md
---

# Simplify raw/ path, add /init command, daily compile schedule

> Superseded by `docs/plans/2026-10-11-001-feat-sitemap-sync-qmd-plugin-plan.md`: output is now namespaced per site (`sites/<site-id>/raw/`) instead of a flat `raw/md/`, and scheduling uses the sync skill instead of a daily compile. The `/init` idea is deferred.

## Overview

Three connected changes to make the knowledge base builder easier to set up and run:

1. **Flatten `raw/` output** — all markdown goes to `raw/md/` instead of `raw/<domain>/`
2. **Add `/init` skill** — one-command setup when user opens Claude Code in the project folder
3. **Add daily compile schedule** — instructions + config for Claude Code / Cowork to auto-compile

## Problem Statement

Current setup has friction:
- `raw/<domain>/` creates a nested path that the compile skill needs to know about
- No onboarding command — user has to read docs and manually run commands
- No automated recompile — user must manually trigger `/compile-wiki` when new content arrives

## Proposed Solution

### 1. Flatten raw output to `raw/md/`

**scrape.py** — change output from `raw/<domain>/` to `raw/md/`:

```python
# Before
output_path = config.output_dir / domain  # raw/example.com/

# After  
output_path = config.output_dir / "md"    # raw/md/
```

**ingest.py** — change output from `raw/` to `raw/md/`:

```python
# Before
class IngestConfig(BaseModel):
    raw_dir: Path = Path("raw")

# After
class IngestConfig(BaseModel):
    raw_dir: Path = Path("raw/md")
```

**compile-wiki skill** — hardcode source dir to `raw/md/`:

```
/compile-wiki raw/md/
```

No more guessing domain names or passing paths.

### 2. Add `/init` skill

New skill at `.claude/skills/init/SKILL.md` that runs when user types `/init`:

```
1. Check prerequisites (Python 3.12+, uv)
2. Run `uv sync` to install dependencies
3. Create directory structure: input/, raw/md/, wiki/
4. Show welcome message with available commands
5. Optionally ask: "Do you want to scrape a website or compile existing files?"
```

**Init flow:**

```
$ claude
> /init

Setting up Knowledge Base Builder...
  [ok] Python 3.12.9
  [ok] uv installed
  [ok] Dependencies installed
  [ok] Directories created: input/, raw/md/, wiki/

Ready! Here's what you can do:

  /scrape-and-compile <sitemap-url>  — Scrape a website and build wiki
  /compile-wiki                       — Compile files already in raw/md/

  Drop files in input/ (md, txt, docx, pdf) then run:
  /scrape-and-compile                 — Ingest + compile

  Or just drop .md files directly in raw/md/ and run:
  /compile-wiki
```

### 3. Two ingest modes

**Mode A: Scrape website**
```
/scrape-and-compile https://example.com/sitemap.xml
```
- Scrapes all pages from sitemap
- Converts HTML to markdown
- Saves to `raw/md/`
- Compiles to wiki

**Mode B: Convert existing folder**
```
/scrape-and-compile
```
- Checks `input/` for files (md, txt, docx, pdf)
- Converts all to `.md`
- Moves to `raw/md/`
- Compiles to wiki

User can also manually drop `.md` files into `raw/md/` and run `/compile-wiki`.

### 4. Daily compile schedule

Add a `.claude/scheduled-tasks/daily-compile/SKILL.md` with instructions for Claude Code / Cowork to run compilation on a cron schedule.

**For Claude Code (local):**

```bash
# Add to .claude/settings.json or use /schedule skill
claude schedule daily-compile "0 9 * * *" "/compile-wiki raw/md/"
```

**For Cowork (cloud):**

Create an Anthropic remote trigger that runs daily:

```json
{
  "name": "daily-compile",
  "schedule": "0 9 * * *",
  "prompt": "/compile-wiki raw/md/"
}
```

**README instructions** will cover both approaches with step-by-step setup.

## Files to Change

### Modify

| File | Change |
|------|--------|
| `scrape.py` | Output to `raw/md/` instead of `raw/<domain>/` |
| `ingest.py` | Output to `raw/md/` instead of `raw/` |
| `.claude/skills/compile-wiki/SKILL.md` | Default source dir = `raw/md/`, no arg required |
| `.claude/skills/scrape-and-compile/SKILL.md` | Update paths, simplify Mode 2/3 |
| `CLAUDE.md` | Update structure diagram and commands |
| `README.md` | Full rewrite: add setup guide, `/init`, daily compile instructions |

### Create

| File | Purpose |
|------|---------|
| `.claude/skills/init/SKILL.md` | `/init` onboarding skill |
| `.gitignore` | Already exists, verify `raw/` and `wiki/` are excluded |

## Acceptance Criteria

- [ ] `scrape.py` writes to `raw/md/` (no domain subfolder)
- [ ] `ingest.py` writes to `raw/md/`
- [ ] `/init` creates directories, installs deps, shows welcome
- [ ] `/compile-wiki` works without arguments (defaults to `raw/md/`)
- [ ] `/scrape-and-compile <url>` scrapes to `raw/md/` then compiles
- [ ] `/scrape-and-compile` (no url) ingests `input/` to `raw/md/` then compiles
- [ ] README has setup guide starting with "open Claude Code, run `/init`"
- [ ] README has daily compile instructions for Claude Code and Cowork
- [ ] No mention of any specific website — uses `example.com` throughout
- [ ] Source links in compiled articles point to `raw/md/<filename>.md`

## Implementation Phases

### Phase 1: Core path changes

1. Update `scrape.py` — change `output_path = config.output_dir / domain` to `config.output_dir / "md"`
2. Update `ingest.py` — change default `raw_dir` to `Path("raw/md")`
3. Update compile-wiki skill — default to `raw/md/`, make source-dir arg optional
4. Update scrape-and-compile skill — remove domain path logic, use `raw/md/`

### Phase 2: Init skill

1. Create `.claude/skills/init/SKILL.md`
2. Prerequisite checks + directory creation + welcome message

### Phase 3: Daily compile + README

1. Add daily compile instructions to README
2. Rewrite README with setup guide flow: clone -> open Claude Code -> `/init` -> go
3. Update CLAUDE.md structure diagram
