# Knowledge Base Builder

A CLI-powered knowledge management system that scrapes websites, converts content to markdown, and compiles a structured wiki with hierarchical index navigation.

## Structure

```
input/          # Drop files here (md, txt, docx, pdf)
raw/            # Processed markdown files (from scrape or ingest)
wiki/           # LLM-maintained knowledge base
  _master-index.md
  _compiled-log.md
  <topic>/
    _index.md
    <article>.md
```

## Commands

- `uv run python scrape.py <sitemap-url>` — Scrape website to raw/
- `uv run python ingest.py` — Convert input/ files to raw/ markdown
- `/compile-wiki raw/<domain>/` — Compile raw files into wiki articles
- `/scrape-and-compile <sitemap-url>` — Full pipeline: scrape + compile

## Rules

- All paths are relative to project root
- Wiki state lives in filesystem (git-friendly)
- No hardcoded absolute paths
- Secrets via environment variables only
