---
name: compile-wiki
description: Internal compile step of a kb-builder sync. Turns one run's queued raw pages into wiki articles using parallel wiki-compiler sub-agents that write only to the run's staging folder. Invoked by the sync and ingest skills as `compile-wiki <site-id> --run-id <run-id>`; not meant to be run on its own.
allowed-tools: Bash(kb-builder:*), Agent
---

# Compile queued pages for one sync run

Arguments: `$ARGUMENTS` — `<site-id> --run-id <run-id>`.

You only produce staged articles. You never read the raw pages yourself, never write into the wiki, never edit indexes, and never move files: `kb-builder sync finalize` does all of that after you finish.

## 1. Get the batches

Run (Bash timeout 600000 ms):

```bash
kb-builder sync batches <site-id> --run-id <run-id>
```

It prints JSON: `taxonomy` (topic folder → label), `total`, and `batches`, each with a `manifest` path and `items` (`slug`, `source_url`, `raw_path`, `output_path`). All paths are absolute. If `total` is 0, stop and report "nothing to compile".

## 2. Spawn compile agents

Use the Agent tool with `subagent_type: kb-builder:wiki-compiler` (it runs on Haiku with only Read and Write). Launch up to 5 at once by putting several Agent calls in a single message (foreground, not background), wait for that round to finish, then launch the next round until every batch has been handed out. One batch per agent.

Prompt for each agent (fill in from the batch; keep every path exactly as given):

```
Compile this batch.

Taxonomy (folder: label):
<TAXONOMY_LINES>

Manifest path: <MANIFEST_PATH>

Items (JSON):
<ITEMS_AS_JSON>
```

Between rounds, run `kb-builder sync batches <site-id> --run-id <run-id>` again only if more than 30 minutes have passed since the last call; it refreshes the run lock's heartbeat.

## 3. Report

When every agent has returned, report how many batches ran and any agent that failed. Do not retry failed batches here: pages without a staged article stay queued and are picked up by the next sync.
