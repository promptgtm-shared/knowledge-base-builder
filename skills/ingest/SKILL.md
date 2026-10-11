---
name: ingest
description: Add local documents to the knowledge base — converts .md, .txt, .docx and .pdf files from the workspace input/ folder into wiki articles in the "local" site and indexes them in qmd. Use when the user wants to add their own files or documents rather than a website.
allowed-tools: Bash(kb-builder:*), Skill, Agent, AskUserQuestion
---

# Ingest local files

Ingest is interactive only: local documents are never compiled by scheduled runs. Run every `kb-builder` command with the Bash timeout set to 600000 ms.

1. Run `kb-builder workspace show`. If no workspace is configured, ask for a folder and run `kb-builder workspace set "<folder>"`. Tell the user to put files in `<workspace>/input/` if it is empty. Files stay there; changed files are recompiled on the next ingest, removed files keep their article.
2. Run `kb-builder ingest`. Exit code 3 with `"status": "skipped"` means a sync is running: report it and stop. Keep `run_id`.
3. If `queue_size` is 0, skip to step 5. Otherwise tell the user how many documents will be compiled and the `estimated_cost_usd`, and ask to proceed. On no, run `kb-builder sync abort --run-id <run_id>` and stop.
4. Invoke the Skill tool with skill `kb-builder:compile-wiki` and args `local --run-id <run_id>`.
5. Run `kb-builder sync finalize local --run-id <run_id>`.
6. Run `kb-builder qmd refresh --run-id <run_id>`, repeating while `"status": "incomplete"` (at most 30 times). This releases the lock.

Report documents compiled, any failures, unsupported files from `fetched`, and the qmd collection `kb-local`. If any command fails unexpectedly, run `kb-builder sync abort --run-id <run_id>` before reporting.
