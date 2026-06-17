from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from frab.api.routes import (
    alerts as alerts_routes,
    coins as coins_routes,
    equity as equity_routes,
    events as events_routes,
    farb_positions as farb_positions_routes,
    funding as funding_routes,
    margin as margin_routes,
    positions as positions_routes,
    signals as signals_routes,
    strategies as strategies_routes,
    wallet as wallet_routes,
    xsmom as xsmom_routes,
)
from frab.events.bus import EventBus


def create_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_bus: EventBus | None = None,
    executor: object | None = None,
    farb_repo: object | None = None,
) -> FastAPI:
    app = FastAPI(title="frab")
    app.state.session_factory = session_factory
    app.state.event_bus = event_bus
    app.state.executor = executor
    app.state.farb_repo = farb_repo
    app.include_router(coins_routes.router, prefix="/api/coins", tags=["coins"])
    app.include_router(strategies_routes.router, prefix="/api/strategies", tags=["strategies"])
    app.include_router(equity_routes.router, prefix="/api/equity", tags=["equity"])
    app.include_router(wallet_routes.router, prefix="/api/equity", tags=["equity"])
    app.include_router(margin_routes.router, prefix="/api/equity/margin", tags=["margin"])
    app.include_router(positions_routes.router, prefix="/api/positions", tags=["positions"])
    app.include_router(farb_positions_routes.router, prefix="/api/farb-positions", tags=["farb-positions"])
    app.include_router(signals_routes.router, prefix="/api/signals", tags=["signals"])
    app.include_router(funding_routes.router, prefix="/api/funding", tags=["funding"])
    app.include_router(events_routes.router, prefix="/api/events", tags=["events"])
    app.include_router(alerts_routes.router, prefix="/api/alerts", tags=["alerts"])
    app.include_router(xsmom_routes.router, prefix="/api/xsmom", tags=["xsmom"])

    if event_bus is not None:
        from frab.api.ws import router as ws_router
        app.include_router(ws_router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app
