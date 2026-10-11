"""`kb` command-line interface."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="Sitemap-driven knowledge base builder.")
site_app = typer.Typer(no_args_is_help=True, help="Register and manage tracked sites.")
sync_app = typer.Typer(no_args_is_help=True, help="Incremental sync steps (prepare, finalize, abort).")
qmd_app = typer.Typer(no_args_is_help=True, help="qmd search index integration.")
app.add_typer(site_app, name="site")
app.add_typer(sync_app, name="sync")
app.add_typer(qmd_app, name="qmd")


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option(envvar="KB_ROOT", help="Knowledge base workspace directory."),
    ] = Path("."),
) -> None:
    ctx.obj = root.expanduser().resolve()


@app.command()
def status(ctx: typer.Context) -> None:
    """Show tracked sites and their sync state."""
    root: Path = ctx.obj
    if not (root / "sites.json").exists():
        typer.echo(f"No sites registered in {root}.")
        return


@app.command()
def ingest(ctx: typer.Context) -> None:
    """Convert files in input/ into the local site."""
    raise typer.Exit(code=1)
