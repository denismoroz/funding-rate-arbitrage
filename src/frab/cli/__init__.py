"""frab CLI — single typer app composed from submodules."""
import typer

from frab.cli import db, serve, smoke
from frab.cli.db import _sync_db_url  # re-export for test compat

app = typer.Typer(no_args_is_help=True, add_completion=False)

db.register(app)
serve.register(app)
smoke.register(app)  # registers live_smoke as add_typer

__all__ = ["app"]
