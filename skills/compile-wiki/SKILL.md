---
name: compile-wiki
description: Internal compile step of a kb-builder sync. Turns one run's queued raw pages into wiki articles using parallel Haiku sub-agents that write only to the run's staging folder. Invoked by the sync skill as `compile-wiki <site-id> --run-id <run-id>`; not meant to be run on its own.
allowed-tools: Bash(kb-builder:*), Agent, Read
---

# Compile queued pages for one sync run

Arguments: `$ARGUMENTS` — `<site-id> --run-id <run-id>`.

You only produce staged articles. You never write into the wiki, never edit indexes, and never move files: `kb-builder sync finalize` does all of that after you finish.

## 1. Get the batches

Run:

```bash
kb-builder sync batches <site-id> --run-id <run-id>
```

It prints JSON: `taxonomy` (topic folder → label), `total`, and `batches`, each with a `manifest` path and `items` (`slug`, `source_url`, `raw_path`, `output_path`). All paths are absolute. If `total` is 0, stop and report "nothing to compile".

## 2. Spawn compile agents

Use the Agent tool with `model: haiku` and `subagent_type: general-purpose`. Launch up to 5 agents at once by putting several Agent calls in a single message (foreground, not background), wait for that round to finish, then launch the next round until every batch has been handed out. One batch per agent.

Fill this prompt for each batch. Paste the taxonomy and the batch's items verbatim; keep every path exactly as given.

```
You are a wiki compiler. For each item below, read the raw markdown file at raw_path and write one wiki article to output_path. Use only the Read and Write tools. Do not create, move or edit any other file.

Topic taxonomy (folder: label). Pick exactly one folder per article; use "general" if nothing fits:
<TAXONOMY_LINES>

Article format (markdown, nothing before the title):

# <Descriptive title>

## Summary

2-3 sentences on what the page covers.

## Key Takeaways

- 3 to 5 concrete takeaways

## Details

The substance of the page in 200-500 words. Plain prose and lists. No links, no images.

## Related

- 2 to 4 related concepts or topics, as plain text

Rules:
- Do not add Source, Created, Updated or Topics lines; they are added later.
- Skip nothing. If a raw file is unreadable, leave it out of the manifest.
- After all articles are written, write the manifest file at <MANIFEST_PATH> as a JSON array, one entry per article you wrote:
  [{"slug": "<slug>", "topic": "<topic folder>"}]

Items:
<ITEMS_AS_JSON>
```

## 3. Report

When every agent has returned, report how many batches ran and any agent that failed or returned an error. Do not retry failed batches here: pages without a staged article stay queued and are picked up by the next sync.
