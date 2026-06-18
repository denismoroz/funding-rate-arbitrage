"""Serve command — runs the shadow-trading engine + FastAPI server."""
import typer
import uvicorn

from frab.settings import get_settings


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "INFO",
    dry_run: bool | None = typer.Option(
        None,
        "--dry-run/--no-dry-run",
        help="Skip executor calls (no real orders). Overrides FRAB_DRY_RUN env var.",
    ),
) -> None:
    """Run shadow-trading engine + FastAPI server on the configured DB."""
    import logging

    from frab.server import build_app

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    settings = get_settings()
    resolved_dry_run = dry_run if dry_run is not None else settings.dry_run
    logging.getLogger(__name__).info(
        "serve: dry_run=%s (source=%s)",
        resolved_dry_run,
        "cli flag" if dry_run is not None else "settings/env",
    )

    # Universe is sourced from coin_registry in the DB — no --coins argument.
    asgi_app = build_app(dry_run=resolved_dry_run)
    uvicorn.run(asgi_app, host=host, port=port, log_level=log_level.lower())


def register(app: typer.Typer) -> None:
    app.command()(serve)
