---
name: compile-wiki
description: Compile raw markdown files into structured wiki articles using parallel Haiku agents. Handles topic classification, article generation, and index building. Invoke with /compile-wiki <source-dir>.
allowed-tools: Agent, Bash, Read, Write, Edit, Glob, Grep, TodoWrite
---

# Compile Wiki from Raw Markdown

## What This Skill Does

Takes a directory of raw markdown files and compiles them into a structured, indexed wiki using parallel Claude Code subagents (Haiku model). Implements the 5-phase compile pipeline from the Knowledge Base System PRD.

## Cost Warning

**IMPORTANT:** Before starting compilation, always warn the user:

> This will spawn multiple Haiku sub-agents to process N files in parallel batches.
> - Using Claude Code subscription: included in your plan, no extra cost.
> - Using Anthropic API directly: estimated ~$0.004 per file (~$X total for N files).
>
> Proceed? (yes/no)

Wait for the user to confirm before spawning any agents. Do NOT skip this step.

## Performance: Use Sub-Agents for Speed

Always spawn multiple sub-agents in parallel to maximize throughput. Never process files sequentially in the main thread. The Agent tool with `run_in_background: true` is the primary execution mechanism.

## Instructions

When the user invokes `/compile-wiki <source-dir>` (e.g., `/compile-wiki raw/clay.com/`):

### Phase 1: Discover and Filter Files

1. Glob all `.md` files from the source directory
2. Read `wiki/_compiled-log.md` if it exists — extract already-processed filenames
3. Filter out:
   - Files already in the compiled log (unless user says "recompile")
   - Files smaller than 200 bytes (too short for meaningful content)
   - Files with names starting with `category-`, `portfolio-tag-`, or `portfolio-category-` (listing pages, not content)
4. Report: "Found N new files to compile (M already processed, K skipped)"
5. If no new files, report "No new files to compile" and stop

### Phase 2: Detect Topic Taxonomy

1. If `wiki/_master-index.md` exists, read it to extract existing topics (folder names and labels)
2. If no master index exists, use this default taxonomy:

```
- mergers-and-acquisitions: Mergers & Acquisitions
- ai-and-technology: AI & Technology
- talent-and-recruitment: Talent & Recruitment
- innovation-and-growth: Innovation & Growth
- localization-industry: Localization Industry
- company-and-events: Company & Events
- leadership-and-strategy: Leadership & Strategy
- content-and-marketing: Content & Marketing
- general: General
```

3. Build the topic list string to embed in agent prompts

### Phase 3: Batch and Spawn Haiku Agents

1. Split the filtered file list into batches of ~40 files each
2. For each batch, spawn an Agent with `model: haiku` and `run_in_background: true`
3. Run up to 5 agents in parallel. If more than 5 batches, wait for the first round to complete before spawning the next round.

**Agent prompt template** (fill in the batch-specific file list and topic taxonomy):

```
You are a wiki compiler. Read markdown files from <SOURCE_DIR> and create structured wiki articles in the wiki/ directory.

## Topic Taxonomy (use EXACTLY these folder names)

<TOPIC_LIST>

## Article Format

For each file, create a wiki article in this EXACT format:

# <Descriptive Title>

**Source:** <SOURCE_DIR>/<original-filename.md>
**Created:** <TODAY_DATE>
**Topics:** <topic-label>

## Summary

2-3 sentence overview. Always reference the original raw document path: "For full content, see <SOURCE_DIR>/<original-filename.md>"

## Key Takeaways

- Takeaway 1
- Takeaway 2
- Takeaway 3

## Details

Expanded content capturing the key substance. 200-500 words. No links.

## Related

- <related-concept-1>
- <related-concept-2>

## Your Batch

Process these files one by one. For each file:
1. Read it from <SOURCE_DIR>/<filename>
2. Determine the best topic from the taxonomy above
3. Generate a wiki article in the format above
4. Write it to wiki/<topic-folder>/<filename>

Files to process:
<FILE_LIST>

After processing all files, write a manifest to wiki/_manifest-batch<N>.md listing:
| Source File | Topic | Wiki Path |
|---|---|---|
| filename.md | topic-folder | wiki/topic-folder/filename.md |

Process ALL files. Do not skip any.
```

### Phase 4: Build Indexes (after all agents complete)

Once all agents finish, run a Python/Bash script to rebuild indexes:

1. **Master index** (`wiki/_master-index.md`):
   - Scan all topic folders in `wiki/`
   - Count articles per topic (excluding `_index.md` and `_manifest-*` files)
   - Write a markdown table with topic name, article count, and folder path

2. **Topic indexes** (`wiki/<topic>/_index.md`):
   - For each topic folder, list all articles
   - Read the first line of each article to get its title
   - Write a sorted list of article links

3. **Compiled log** (`wiki/_compiled-log.md`):
   - Read existing log if present
   - Append all newly processed files with today's date
   - Include skipped files with reasons

Use this Bash/Python approach:

```bash
uv run python -c "
from pathlib import Path
from datetime import date
# ... index building logic
"
```

Or inline the logic directly with Bash + Read/Write tools.

### Phase 5: Cross-link Validation

1. Scan all wiki articles for `## Related` sections
2. For articles with fewer than 2 related items, find articles in the same topic and suggest additions
3. This phase is best-effort and non-blocking — don't fail the compile if cross-linking has issues

### Reporting

After all phases complete, print:
```
Compile complete!
- Articles created: N
- Articles skipped: M (already compiled or too short)
- Topics: K
- Master index: updated
- Compiled log: updated
```

## Incremental Behavior

- The compiled log (`wiki/_compiled-log.md`) tracks every processed file
- On subsequent runs, only NEW files (not in the log) are processed
- The user can force recompile by saying "recompile" — this ignores the log
- Indexes are always rebuilt from scratch (they self-heal)

## Error Handling

- If an agent fails or times out, log which files it was processing and continue with other batches
- If a source file can't be read, skip it with a warning
- Never let one file failure stop the entire compile

## Recompile

If the user says `/compile-wiki recompile <source-dir>`:
- Ignore the compiled log
- Reprocess all files from scratch
- Rebuild all indexes
