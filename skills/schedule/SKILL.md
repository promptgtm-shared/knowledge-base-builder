---
name: schedule
description: Schedule recurring unattended syncs for tracked sites using a Claude desktop scheduled task, or stop scheduling a site. Use when the user wants a site checked daily/weekly/hourly, asks to automate syncing, or wants to pause scheduled syncs.
argument-hint: "<site-id|all> [off]"
allowed-tools: Bash(kb-builder:*), AskUserQuestion, mcp__scheduled-tasks__list_scheduled_tasks, mcp__scheduled-tasks__create_scheduled_task, mcp__scheduled-tasks__update_scheduled_task
---

# Schedule syncs

Arguments: `$ARGUMENTS` — a site id or `all`, optionally followed by `off`.

One scheduled task, `kb-builder-sync`, runs `/kb-builder:sync all --unattended`, and that syncs every site flagged `scheduled`. Scheduling a site means setting its flag and making sure the task exists.

## Turn scheduling off

For `<site-id> off`: run `kb-builder site update <site-id> --unscheduled`. If `kb-builder site list --json --scheduled` now returns an empty list, offer to disable the `kb-builder-sync` task with `update_scheduled_task` (`enabled: false`). Stop here.

## Turn scheduling on

1. Run `kb-builder site update <site-id> --scheduled` for the site (or each listed site for `all`, skipping `local`). If it refuses because the first sync has not run, tell the user to run `/kb-builder:sync <site-id>` first and stop.
2. Call `list_scheduled_tasks`. If `kb-builder-sync` exists and is enabled, report that the site will be included from the next run and stop.
3. Ask how often to sync, with a default of daily at 07:00. Map the answer to a 5-field cron expression in local time (daily `0 7 * * *`, weekdays `0 7 * * 1-5`, every 6 hours `0 */6 * * *`, weekly Monday `0 7 * * 1`).
4. Create the task with `create_scheduled_task`:
   - `taskId`: `kb-builder-sync`
   - `title`: `Knowledge base sync`
   - `description`: `Sync scheduled kb-builder sites and refresh qmd`
   - `cronExpression`: from step 3
   - `prompt`: `/kb-builder:sync all --unattended`
5. Tell the user, in this order:
   - Runs happen while the Claude desktop app is open and the computer is awake; a missed run catches up when the app opens again.
   - Click **Run now** on the task once (Scheduled section in the sidebar) and approve the tools it asks for. Approvals are saved on the task, so later runs do not stop on permission prompts.
   - Each run compiles at most the workspace cap in total (default 50 pages; change it with `kb-builder workspace cap <N>`); leftovers continue on the next run.
   - When approving tools for the task, approve `kb-builder` commands, Skill and Agent, and approve file writes only inside the workspace folder. The compile agents only need to write into `<workspace>/sites/*/staging/`.
   - Problems show at the top of each run's final message (lines starting with `PROBLEM:`) and in `/kb-builder:status`.

## Without the desktop app

If the user wants syncs without the desktop app open, explain the headless alternative and let them set it up themselves. It needs a valid `claude` CLI login (`claude auth login`). Create a Windows Task Scheduler task or cron entry, with the workspace folder as the working directory, that runs:

```bash
claude -p "/kb-builder:sync all --unattended" --add-dir "<workspace>" --permission-mode dontAsk --allowedTools "Bash(kb-builder:*)" "Skill" "Agent" "Read(//<workspace>/**)" "Edit(//<workspace>/sites/**)"
```

`//` marks an absolute path in permission rules; Edit rules also cover writes. Keep file access scoped to the workspace: the compile agents read untrusted web content.

Use the absolute path to `claude`. Do not use `--bare`: it skips installed plugins.
