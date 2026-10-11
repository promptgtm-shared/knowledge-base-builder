# Knowledge Base Builder

Claude Code plugin (`kb-builder`) that tracks websites by sitemap, incrementally compiles changed pages into a markdown wiki with Haiku sub-agents, and indexes each site's wiki as a qmd collection.

## Structure

```
.claude-plugin/   # plugin.json + marketplace.json (repo is its own marketplace)
bin/kb-builder    # wrapper skills call; runs the kb CLI from the locked uv env
skills/           # add-site, sync, compile-wiki (internal), schedule, status, ingest
src/kb/           # kb CLI: config, urls, http, sitemap, fetch, state, diff, locks,
                  #   sync (prepare/abort/batches/ingest), finalize, indexes, qmd, report
tests/            # pytest; httpx mocked with respx, qmd with a fake runner
docs/plans/       # implementation plans
```

User data never lives in the repo or plugin root: it is in the workspace folder saved by `kb workspace set` (see README "Workspace layout").

## Commands

- `uv run pytest` — test suite
- `uv run kb --root <tmp> ...` — run the CLI against a scratch workspace
- `KB_QMD_INDEX=<name>` — point qmd calls at a throwaway named index when testing against real qmd
- `claude plugin validate .` — validate manifests

## Rules

- Deterministic work belongs in the `kb` CLI; skills only orchestrate and run the compile agents.
- `finalize` is the only writer of `wiki/` and `archive/`; compile agents write to `staging/<run-id>/` only.
- Never call `qmd update` more than once per run or when nothing changed (it reindexes every collection and runs their hooks).
- CLI output consumed by skills is JSON; keep field names stable.
- No hardcoded absolute paths; secrets via environment variables only.
- Example domains in docs and tests: `example.com` only.
