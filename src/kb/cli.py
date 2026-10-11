"""`kb` command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from kb import config, locks, sync

app = typer.Typer(
    no_args_is_help=True,
    help="Sitemap-driven knowledge base builder.",
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
site_app = typer.Typer(no_args_is_help=True, help="Register and manage tracked sites.")
sync_app = typer.Typer(no_args_is_help=True, help="Incremental sync steps (prepare, finalize, abort).")
workspace_app = typer.Typer(no_args_is_help=True, help="Choose the workspace folder.")
qmd_app = typer.Typer(no_args_is_help=True, help="qmd search index integration.")
app.add_typer(site_app, name="site")
app.add_typer(sync_app, name="sync")
app.add_typer(qmd_app, name="qmd")
app.add_typer(workspace_app, name="workspace")


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=2)


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path | None,
        typer.Option(envvar="KB_ROOT", help="Workspace directory (default: the one saved with `kb workspace set`)."),
    ] = None,
) -> None:
    ctx.obj = config.resolve_root(root)


def _root(ctx: typer.Context) -> Path:
    if ctx.obj is None:
        _fail("No workspace configured. Run `kb workspace set <folder>` or pass --root.")
    return ctx.obj


@workspace_app.command("set")
def workspace_set(path: Path) -> None:
    """Remember the workspace folder used when --root is not given."""
    root = config.set_default_root(path)
    typer.echo(f"Workspace set to {root}")


@workspace_app.command("show")
def workspace_show(ctx: typer.Context) -> None:
    """Print the active workspace folder."""
    typer.echo(str(_root(ctx)))


@app.command()
def status(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Show tracked sites, queue sizes, held archives and the last run."""
    root = _root(ctx)
    statuses = sync.site_statuses(root)
    if as_json:
        typer.echo(json.dumps([s.model_dump() for s in statuses], indent=2))
        return
    if not statuses:
        typer.echo(f"No sites registered in {root}.")
        return
    lock = locks.read_lock(config.WorkspacePaths(root).lock_file)
    if lock and not locks.is_stale(lock):
        typer.echo(f"Sync in progress: run {lock.run_id} ({lock.label}).")
    for s in statuses:
        flags = ", ".join(f for f, on in (("scheduled", s.scheduled), ("qmd pending", s.qmd_dirty)) if on)
        typer.echo("")
        typer.echo(f"{s.id} - {s.source}" + (f" [{flags}]" if flags else ""))
        typer.echo(f"  last sync: {s.last_sync_at or 'never'}")
        typer.echo("  pages: " + (", ".join(f"{k} {v}" for k, v in sorted(s.pages.items())) or "none"))
        if s.queued:
            typer.echo(f"  queued for compile: {s.queued}" + (f" ({s.churning} churning)" if s.churning else ""))
        if s.archive_held:
            typer.echo(f"  HELD ARCHIVE: {s.archive_held} pages vanished at once; review, then sync {s.id} --approve-archive")
        if s.last_run:
            typer.echo(f"  last run: {s.last_run.get('status')} - {s.last_run.get('report')}")


@app.command()
def ingest(
    ctx: typer.Context,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Join an existing run.")] = None,
) -> None:
    """Convert files in input/ into the local site and queue new or changed ones (JSON)."""
    try:
        result = sync.prepare_local(_root(ctx), run_id)
    except locks.LockLost as e:
        _fail(str(e))
    except locks.LockHeld as e:
        typer.echo(json.dumps({"status": "skipped", "message": str(e)}))
        raise typer.Exit(code=3) from None
    _emit(result)


@site_app.command("add")
def site_add(
    ctx: typer.Context,
    sitemap_url: str,
    site_id: Annotated[str | None, typer.Option("--id", help="Override the site id derived from the domain.")] = None,
    description: Annotated[str, typer.Option(help="Short description, used as qmd context.")] = "",
) -> None:
    """Register a site by its sitemap URL."""
    try:
        site = config.add_site(_root(ctx), sitemap_url, site_id=site_id, description=description)
    except config.ConfigError as e:
        _fail(str(e))
    typer.echo(f"Registered '{site.id}' -> {config.WorkspacePaths(_root(ctx)).site(site.id).root}")


@site_app.command("list")
def site_list(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
    scheduled: Annotated[bool, typer.Option("--scheduled", help="Only sites included in unattended runs.")] = False,
    sitemap_only: Annotated[bool, typer.Option("--sitemap-only", help="Exclude the local ingest site.")] = False,
) -> None:
    """List registered sites."""
    sites = [
        s
        for s in config.load_workspace(_root(ctx)).sites.values()
        if (not scheduled or s.scheduled) and (not sitemap_only or not s.is_local)
    ]
    if as_json:
        typer.echo(json.dumps([s.model_dump(mode="json") for s in sites], indent=2))
        return
    for site in sites:
        flags = [f for f, on in (("scheduled", site.scheduled), ("first-sync-done", site.first_sync_complete)) if on]
        typer.echo(f"{site.id}	{site.sitemap_url or '-'}	{','.join(flags)}")


@site_app.command("show")
def site_show(ctx: typer.Context, site_id: str) -> None:
    """Print a site's configuration as JSON."""
    try:
        site = config.get_site(config.load_workspace(_root(ctx)), site_id)
    except config.ConfigError as e:
        _fail(str(e))
    typer.echo(json.dumps(site.model_dump(mode="json"), indent=2))


@site_app.command("update")
def site_update(
    ctx: typer.Context,
    site_id: str,
    description: Annotated[str | None, typer.Option(help="New description.")] = None,
    taxonomy_file: Annotated[
        Path | None, typer.Option(help="JSON file mapping topic folder -> label; replaces the taxonomy.")
    ] = None,
    scheduled: Annotated[bool | None, typer.Option("--scheduled/--unscheduled", help="Include in unattended runs.")] = None,
) -> None:
    """Update a site's description, taxonomy, or schedule flag."""
    try:
        site = config.get_site(config.load_workspace(_root(ctx)), site_id)
        if description is not None:
            site.description = description
        if taxonomy_file is not None:
            taxonomy = json.loads(taxonomy_file.read_text(encoding="utf-8"))
            if not isinstance(taxonomy, dict) or not taxonomy:
                raise config.ConfigError("Taxonomy file must be a non-empty JSON object.")
            taxonomy.setdefault("general", "General")
            site.taxonomy = {str(k): str(v) for k, v in taxonomy.items()}
        if scheduled is not None:
            if scheduled and not site.first_sync_complete:
                raise config.ConfigError("Run the first sync interactively before scheduling this site.")
            site.scheduled = scheduled
        config.update_site(_root(ctx), site)
    except config.ConfigError as e:
        _fail(str(e))
    typer.echo(f"Updated '{site_id}'.")


@site_app.command("remove")
def site_remove(
    ctx: typer.Context,
    site_id: str,
    purge: Annotated[bool, typer.Option("--purge", help="Also delete the site's folder and qmd collection.")] = False,
) -> None:
    """Deregister a site (files are kept unless --purge)."""
    from kb.qmd import Qmd, QmdError, collection_name

    try:
        config.remove_site(_root(ctx), site_id, purge=purge)
    except config.ConfigError as e:
        _fail(str(e))
    if purge:
        try:
            if Qmd().remove_collection(collection_name(site_id)):
                typer.echo(f"Removed qmd collection {collection_name(site_id)}.")
        except QmdError as e:
            typer.echo(f"Warning: could not remove qmd collection: {e}", err=True)
    typer.echo(f"Removed '{site_id}'" + (" and deleted its files." if purge else " (files kept)."))


def _emit(model) -> None:
    typer.echo(model.model_dump_json(indent=2))


@sync_app.command("prepare")
def sync_prepare(
    ctx: typer.Context,
    site_id: str,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Continue or join an existing run.")] = None,
    full: Annotated[bool, typer.Option("--full", help="Ignore lastmod and re-check every page's content.")] = False,
    approve_archive: Annotated[
        bool, typer.Option("--approve-archive", help="Release a held mass-archive (interactive runs only).")
    ] = False,
    unattended: Annotated[bool, typer.Option("--unattended", help="Apply the workspace compile cap.")] = False,
    already_queued: Annotated[
        int, typer.Option("--already-queued", help="Pages already queued by earlier sites in this unattended run.")
    ] = 0,
    budget: Annotated[
        int | None, typer.Option("--budget", help="Compile at most this many pages now (others stay queued).")
    ] = None,
    time_budget: Annotated[float, typer.Option("--time-budget", help="Seconds before returning 'incomplete'.")] = sync.DEFAULT_TIME_BUDGET_S,
) -> None:
    """Discover, diff and fetch a site; print JSON with status incomplete | ready | failed."""
    root = _root(ctx)
    if unattended:
        budget = sync.remaining_budget(root, already_queued)
    try:
        result = sync.prepare(
            root,
            site_id,
            run_id,
            full=full,
            approve_archive=approve_archive and not unattended,
            budget=budget,
            time_budget_s=time_budget,
        )
    except (config.ConfigError, locks.LockLost) as e:
        _fail(str(e))
    except locks.LockHeld as e:
        typer.echo(json.dumps({"status": "skipped", "message": str(e)}))
        raise typer.Exit(code=3) from None
    _emit(result)


@sync_app.command("abort")
def sync_abort(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option("--run-id", help="Run to abort.")],
) -> None:
    """Release the workspace lock and discard this run's staging."""
    released = sync.abort(_root(ctx), run_id)
    typer.echo("Run aborted; lock released." if released else "Run aborted; lock was not held by this run.")


@sync_app.command("finalize")
def sync_finalize(
    ctx: typer.Context,
    site_id: str,
    run_id: Annotated[str, typer.Option("--run-id", help="Run whose staged articles to publish.")],
) -> None:
    """Publish staged articles, archive/restore pages, rebuild indexes; print JSON."""
    from kb.finalize import FinalizeError, finalize

    try:
        result = finalize(_root(ctx), site_id, run_id)
    except (config.ConfigError, locks.LockLost, FinalizeError) as e:
        _fail(str(e))
    _emit(result)


@qmd_app.command("refresh")
def qmd_refresh(
    ctx: typer.Context,
    run_id: Annotated[str, typer.Option("--run-id", help="Run to finish; the lock is released when done.")],
    time_budget: Annotated[float, typer.Option("--time-budget", help="Seconds before returning 'incomplete'.")] = 480.0,
) -> None:
    """Index and embed every site whose wiki changed; print JSON with status done | incomplete | failed | skipped."""
    from kb.qmd import refresh

    try:
        result = refresh(_root(ctx), run_id, time_budget_s=time_budget)
    except locks.LockLost as e:
        _fail(str(e))
    _emit(result)


@sync_app.command("batches")
def sync_batches(
    ctx: typer.Context,
    site_id: str,
    run_id: Annotated[str, typer.Option("--run-id", help="Run whose queue to split.")],
    size: Annotated[int, typer.Option("--size", help="Pages per compile agent.")] = 25,
) -> None:
    """Print this run's compile queue as batches (JSON) for the compile agents."""
    try:
        plan = sync.plan_batches(_root(ctx), site_id, run_id, size)
    except config.ConfigError as e:
        _fail(str(e))
    _emit(plan)
