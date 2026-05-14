from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.api.routes import (
    equity as equity_routes,
    events as events_routes,
    funding as funding_routes,
    positions as positions_routes,
    signals as signals_routes,
    strategies as strategies_routes,
)


def create_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="frab")
    app.state.session_factory = session_factory
    app.include_router(strategies_routes.router, prefix="/api/strategies", tags=["strategies"])
    app.include_router(equity_routes.router, prefix="/api/equity", tags=["equity"])
    app.include_router(positions_routes.router, prefix="/api/positions", tags=["positions"])
    app.include_router(signals_routes.router, prefix="/api/signals", tags=["signals"])
    app.include_router(funding_routes.router, prefix="/api/funding", tags=["funding"])
    app.include_router(events_routes.router, prefix="/api/events", tags=["events"])

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app
