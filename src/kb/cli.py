"""`kb` command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from kb import config

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


def _fail(message: str) -> None:
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
def status(ctx: typer.Context) -> None:
    """Show tracked sites and their sync state."""
    root = _root(ctx)
    ws = config.load_workspace(root)
    if not ws.sites:
        typer.echo(f"No sites registered in {root}.")
        return
    for site in ws.sites.values():
        typer.echo(f"{site.id}: {site.sitemap_url or 'local files'}")


@app.command()
def ingest(ctx: typer.Context) -> None:
    """Convert files in input/ into the local site."""
    _fail("ingest is not available yet.")


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
def site_list(ctx: typer.Context) -> None:
    """List registered sites."""
    ws = config.load_workspace(_root(ctx))
    for site in ws.sites.values():
        flags = [f for f, on in (("scheduled", site.scheduled), ("first-sync-done", site.first_sync_complete)) if on]
        typer.echo(f"{site.id}\t{site.sitemap_url or '-'}\t{','.join(flags)}")


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
    try:
        config.remove_site(_root(ctx), site_id, purge=purge)
    except config.ConfigError as e:
        _fail(str(e))
    typer.echo(f"Removed '{site_id}'" + (" and deleted its files." if purge else " (files kept)."))
