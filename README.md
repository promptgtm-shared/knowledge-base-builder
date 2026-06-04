# Knowledge Base Builder

Turn any website's sitemap into a structured, searchable wiki — in minutes, not months.

## What it does

Point it at a sitemap URL. It scrapes every page, converts to markdown, classifies content into topics, and compiles structured wiki articles with summaries, key takeaways, and cross-references.

**Built for [Claude Code](https://docs.anthropic.com/en/docs/claude-code).**

## Results

| Metric | Value |
|--------|-------|
| Pages processed | 10,782 |
| Wiki articles created | 10,475 |
| Topics auto-classified | 11 |
| Total time | ~90 minutes |
| Manual equivalent | 898 hours (112 working days) |
| Speedup | **~600x** |

## How it works

```
sitemap.xml
    |
    v
scrape.py (concurrent, rate-limited)
    |
    v
raw/<domain>/*.md
    |
    v
/compile-wiki (parallel Haiku sub-agents)
    |
    v
wiki/<topic>/*.md (structured articles + indexes)
```

### Pipeline phases

1. **Scrape** — Fetches all pages from a sitemap XML (supports nested sitemaps). Concurrent with rate limiting and retry logic. Converts HTML to clean markdown.

2. **Ingest** — Converts local files (docx, pdf, txt) dropped in `input/` to markdown.

3. **Compile** — Spawns parallel Haiku sub-agents that read raw files, classify topics, and write structured wiki articles. Each article gets a summary, key takeaways, detailed content, and related concepts.

4. **Index** — Builds a master index, per-topic indexes, and a compiled log. All indexes are idempotent and self-healing.

5. **Cross-link** — Validates that articles reference related content.

## Quick start

### Prerequisites

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

### Setup

```bash
git clone https://github.com/promptgtm/knowledge-base-builder.git
cd knowledge-base-builder
uv sync
```

### Usage

#### Full pipeline (scrape + compile)

In Claude Code:

```
/scrape-and-compile https://example.com/sitemap.xml
```

#### Compile only (from existing raw files)

```
/compile-wiki raw/example.com/
```

#### Ingest local files + compile

Drop `.md`, `.txt`, `.docx`, or `.pdf` files into `input/`, then:

```
/scrape-and-compile
```

### CLI scraper (standalone)

```bash
uv run python scrape.py https://example.com/sitemap.xml

# With options
uv run python scrape.py https://example.com/sitemap.xml \
  -c 50 \
  --min-delay 0.1 \
  --max-delay 0.5
```

### CLI ingest (standalone)

```bash
uv run python ingest.py
```

## Project structure

```
knowledge-base-builder/
  scrape.py              # Sitemap scraper (async, concurrent)
  ingest.py              # File converter (docx, pdf, txt -> markdown)
  pyproject.toml         # Dependencies
  .claude/
    skills/
      compile-wiki/      # Wiki compilation skill (parallel Haiku agents)
      scrape-and-compile/ # End-to-end pipeline skill
  input/                 # Drop files here for ingestion
  raw/                   # Scraped/ingested markdown (intermediate)
  wiki/                  # Compiled knowledge base (output)
    _master-index.md     # Topic overview with article counts
    _compiled-log.md     # Processing log
    <topic>/
      _index.md          # Topic article listing
      <article>.md       # Structured wiki article
```

## Wiki article format

Every compiled article follows this structure:

```markdown
# Descriptive Title

**Source:** [original-file.md](../../raw/example.com/original-file.md)
**Created:** 2026-04-14
**Topics:** Topic Name

## Summary
2-3 sentence overview.

## Key Takeaways
- Takeaway 1
- Takeaway 2
- Takeaway 3

## Details
Expanded content. 200-500 words. No links.

## Related
- Related concept 1
- Related concept 2
```

## Configuration

### Scraper options

| Flag | Default | Description |
|------|---------|-------------|
| `-c, --concurrency` | 10 | Max concurrent requests |
| `--min-delay` | 0.5 | Min delay between requests (seconds) |
| `--max-delay` | 2.0 | Max delay between requests (seconds) |
| `--max-retries` | 3 | Max retries per page |
| `-o, --output-dir` | `raw` | Output directory |

### Compile tuning

Edit `.claude/skills/compile-wiki/SKILL.md` to customize:

- **Batch size** — Files per sub-agent (default: ~40)
- **Parallel agents** — Max concurrent agents (default: 5)
- **Topic taxonomy** — Add/remove topic categories
- **Article format** — Modify the wiki article template
- **Minimum file size** — Skip files smaller than N bytes (default: 200)

## Key lessons from production use

1. **Use absolute paths in agent prompts.** Sub-agents can lose working directory context.

2. **Some agents process entire directories.** When given a batch of 100 dossier files, one agent compiled all 5,000+ in the directory. This is beneficial — let greedy agents run.

3. **Watch for multiple file prefixes per category.** `integrations-action-*` and `integrations-data-points-*` are both "integrations" but need separate batch handling.

4. **Indexes are self-healing.** Run the index builder after any compile pass. It scans the filesystem and rebuilds from scratch.

5. **Increase concurrency for large sites.** Default scraper concurrency (10) is conservative. For 10K+ pages, bump to 50 with reduced delays.

## Dependencies

- [httpx](https://www.python-httpx.org/) — Async HTTP client with HTTP/2
- [beautifulsoup4](https://www.crummy.com/software/BeautifulSoup/) — HTML parsing
- [html2text](https://github.com/Alir3z4/html2text/) — HTML to markdown
- [rich](https://rich.readthedocs.io/) — Terminal UI
- [pydantic](https://docs.pydantic.dev/) — Config validation
- [python-docx](https://python-docx.readthedocs.io/) — DOCX conversion
- [pymupdf](https://pymupdf.readthedocs.io/) — PDF extraction

## License

MIT
