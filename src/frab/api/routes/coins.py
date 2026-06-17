"""Coin Registry API — CRUD endpoints for the FRAB coin_registry table.

Endpoints:
  GET    /coins                  — list all registry rows
  POST   /coins                  — add a coin (discover + validate via HL, active=False)
  PATCH  /coins/{coin}           — edit risk fields only
  POST   /coins/{coin}/active    — toggle active flag
  DELETE /coins/{coin}           — remove (guarded: no open farb_position)

Cache-reload semantics (CRITICAL — avoids the XSMOM stale-params footgun):
  After EVERY successful mutation, ``app.state.coin_registry.reload()`` is
  called so the in-memory CoinRegistry snapshot is refreshed immediately.
  Callers that hold a reference to the same object (server.py stashes it as
  ``app.state.coin_registry``) will see the new values on their next sync
  accessor call.

Live-pickup note for newly-activated coins:
  ``EngineLoop`` captures ``coins=list(active_coins)`` at construction time
  (server.py lifespan, passed to ``EngineLoop.__init__``).  The loop stores
  this as ``self._coins`` and uses it for every minute-tick quote fetch and
  equity snapshot.  It does NOT re-read ``registry.universe()`` per cycle.

  Consequence: activating a coin via POST /coins/{coin}/active refreshes
  ``CoinRegistry.universe()`` immediately (because reload() is called), but the
  running EngineLoop will NOT start quoting or taking positions on it until the
  process is restarted.

  Recommendation for a future phase: pass the ``CoinRegistry`` object into
  ``EngineLoop`` and have ``_minute_tick`` call ``registry.universe()`` to
  re-derive ``self._coins`` each cycle.  That change is low-risk but not
  required for correctness today — the registry is the single source of truth
  for what *should* trade; the loop just needs a restart to pick up the new set.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from frab.coin_discovery import CoinDiscovery
from frab.repo.coin_registry_repo import CoinEntry, CoinRegistryRepo

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Market-fact fields — NOT editable via PATCH ───────────────────────────────
_MARKET_FACT_FIELDS: frozenset[str] = frozenset({
    "spot_token",
    "sz_decimals",
    "bridge_safe",
    "validated_at",
    "coin",
})

# ── Pydantic response / request models ────────────────────────────────────────


class CoinRow(BaseModel):
    """Full coin_registry row as returned by the API."""
    coin: str
    leverage: int
    maint_ratio: float
    position_size_usd: float | None
    active: bool
    spot_token: str | None
    sz_decimals: int | None
    bridge_safe: bool
    validated_at: int | None  # epoch ms; None = not validated

    @classmethod
    def from_entry(cls, entry: CoinEntry) -> "CoinRow":
        return cls(
            coin=entry.coin,
            leverage=entry.leverage,
            maint_ratio=entry.maint_ratio,
            position_size_usd=entry.position_size_usd,
            active=entry.active,
            spot_token=entry.spot_token,
            sz_decimals=entry.sz_decimals,
            bridge_safe=entry.bridge_safe,
            validated_at=entry.validated_at,
        )


class AddCoinRequest(BaseModel):
    """Body for POST /coins."""
    coin: str
    leverage: int
    maint_ratio: float
    position_size_usd: float | None = None


class PatchCoinRequest(BaseModel):
    """Body for PATCH /coins/{coin}.

    Only risk PARAMETERS are accepted (leverage, maint_ratio,
    position_size_usd).  The ``active`` flag is intentionally NOT editable
    here — activation goes through POST /coins/{coin}/active, which enforces
    the validated_at gate.  Allowing active=True via PATCH would bypass that
    gate and could create an (active=True, validated_at=NULL) row that the
    startup validation gate (Phase C) refuses to boot on.

    Market-fact fields (spot_token, sz_decimals, bridge_safe, validated_at,
    coin) are rejected with 422 via the endpoint's explicit check.
    """

    model_config = {"extra": "allow"}

    leverage: int | None = None
    maint_ratio: float | None = None
    position_size_usd: float | None = None


class SetActiveRequest(BaseModel):
    """Body for POST /coins/{coin}/active."""
    active: bool


# ── DI helpers ────────────────────────────────────────────────────────────────

def _get_repo(request: Request) -> CoinRegistryRepo:
    """Build CoinRegistryRepo from the session_factory on app.state."""
    return CoinRegistryRepo(request.app.state.session_factory)


def _get_coin_registry(request: Request):
    """Return the CoinRegistry service from app.state (may be None in tests)."""
    return getattr(request.app.state, "coin_registry", None)


def _get_discovery(request: Request) -> CoinDiscovery:
    """Build CoinDiscovery from the FRAB exchange client on app.state.

    Raises 503 if neither the FRAB exchange nor the coin_discovery object
    is available (e.g. engine not started in this process).
    """
    # Prefer a pre-built CoinDiscovery stashed on app.state (test injection point)
    discovery = getattr(request.app.state, "coin_discovery", None)
    if discovery is not None:
        return discovery

    exchange = getattr(request.app.state, "exchange", None)
    if exchange is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "FRAB exchange not configured; cannot perform HL discovery. "
                "Start the full server (frab serve) or inject coin_discovery on app.state."
            ),
        )
    client = exchange._client  # HLExchange wraps an HLClient as _client
    repo = _get_repo(request)
    return CoinDiscovery(client=client, repo=repo)


async def _reload_registry(request: Request) -> None:
    """Reload the CoinRegistry snapshot if it is stashed on app.state.

    No-op when coin_registry is absent (tests that don't set it up).
    """
    registry = _get_coin_registry(request)
    if registry is not None:
        await registry.reload()


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("")
async def list_coins(request: Request) -> list[CoinRow]:
    """List all coin_registry rows (risk fields + market facts + active + validated_at)."""
    repo = _get_repo(request)
    entries = await repo.list()
    return [CoinRow.from_entry(e) for e in entries]


@router.post("", status_code=201)
async def add_coin(body: AddCoinRequest, request: Request) -> CoinRow:
    """Add a coin by ticker.

    Runs HL discovery + validation (CoinDiscovery.validate_and_register).
    The coin is created with active=False; enable it separately via
    POST /coins/{coin}/active once you have reviewed the discovered facts.

    On discovery or validation failure → 422 with the error message.
    No row is written on failure (CoinDiscovery guarantees atomicity).
    """
    discovery = _get_discovery(request)
    try:
        entry = await discovery.validate_and_register(
            body.coin,
            leverage=body.leverage,
            maint_ratio=body.maint_ratio,
            position_size_usd=body.position_size_usd,
            active=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _reload_registry(request)
    return CoinRow.from_entry(entry)


@router.patch("/{coin}")
async def patch_coin(coin: str, body: PatchCoinRequest, request: Request) -> CoinRow:
    """Edit risk parameters for a coin.

    Editable: leverage, maint_ratio, position_size_usd. The ``active`` flag is
    NOT editable here (use POST /coins/{coin}/active, which enforces the
    validated_at gate). Market-fact fields (spot_token, sz_decimals,
    bridge_safe, validated_at) are NOT editable here; include any of these or
    ``active`` in the body → 422.

    Guard: changing leverage or maint_ratio while a non-terminal FarbPosition
    exists for this coin → 409 Conflict (unsafe to change margin params under
    an open position).

    position_size_usd-only changes are NOT guarded.
    """
    coin = coin.upper()

    # ── Reject market-fact fields in the body ──────────────────────────────
    # model_config extra="allow" captures unknown keys; check against our
    # blacklist and raise 422 immediately (they are not user-editable here).
    extra_keys: set[str] = set(body.model_extra or {})
    bad_keys = extra_keys & _MARKET_FACT_FIELDS
    if bad_keys:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Market-fact fields are not editable via PATCH: {sorted(bad_keys)}. "
                "These fields are populated by HL discovery (POST /coins). "
                "To update them, remove the coin and re-add it."
            ),
        )

    # ── Reject 'active' in PATCH — activation has its own gated endpoint ────
    if "active" in extra_keys:
        raise HTTPException(
            status_code=422,
            detail=(
                "'active' is not editable via PATCH (it would bypass the "
                "validated_at gate). Use POST /coins/{coin}/active instead."
            ),
        )

    repo = _get_repo(request)

    entry = await repo.get(coin)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Coin {coin!r} not found in registry")

    # ── Guard: leverage / maint_ratio change with open position ────────────
    changing_margin_params = (
        (body.leverage is not None and body.leverage != entry.leverage)
        or (body.maint_ratio is not None and body.maint_ratio != entry.maint_ratio)
    )
    if changing_margin_params:
        has_open = await repo.has_open_position(coin)
        if has_open:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot change leverage or maint_ratio for {coin!r}: "
                    "a non-terminal FarbPosition exists for this coin. "
                    "Changing margin parameters under an open position is unsafe. "
                    "Wait for the position to close or force-close it first."
                ),
            )

    # ── Apply updates ──────────────────────────────────────────────────────
    new_leverage = body.leverage if body.leverage is not None else entry.leverage
    new_maint_ratio = body.maint_ratio if body.maint_ratio is not None else entry.maint_ratio
    # position_size_usd: allow explicit None to clear the value
    new_position_size_usd = (
        body.position_size_usd if body.position_size_usd is not None else entry.position_size_usd
    )

    updated = await repo.upsert(
        coin,
        leverage=new_leverage,
        maint_ratio=new_maint_ratio,
        position_size_usd=new_position_size_usd,
        active=entry.active,  # active is preserved; change it via POST /active
        # Market-fact fields are preserved from the existing row
        spot_token=entry.spot_token,
        sz_decimals=entry.sz_decimals,
        bridge_safe=entry.bridge_safe,
        validated_at=entry.validated_at,
    )

    await _reload_registry(request)
    return CoinRow.from_entry(updated)


@router.post("/{coin}/active")
async def set_coin_active(coin: str, body: SetActiveRequest, request: Request) -> CoinRow:
    """Enable or disable a coin in the trading universe.

    Enabling (active=True) requires the coin to have validated_at set.
    A coin added via POST /coins already has validated_at set (CoinDiscovery
    sets it).  A row manually inserted without discovery (validated_at IS NULL)
    cannot be activated → 409.

    Disabling (active=False) is always allowed regardless of open positions.
    (Open positions on a disabled coin are managed by the strategy's normal
    exit path — this endpoint does NOT force-close them.)

    Note on live pickup: calling this endpoint reloads CoinRegistry immediately
    so ``app.state.coin_registry.universe()`` reflects the change.  However,
    the running EngineLoop captured its coin list at startup and will NOT start
    trading a newly-activated coin until the process is restarted.
    """
    coin = coin.upper()
    repo = _get_repo(request)

    entry = await repo.get(coin)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Coin {coin!r} not found in registry")

    if body.active and entry.validated_at is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot activate {coin!r}: validated_at is NULL. "
                "The coin must be validated via POST /coins (HL discovery) before activation."
            ),
        )

    updated = await repo.set_active(coin, body.active)
    await _reload_registry(request)
    return CoinRow.from_entry(updated)


@router.delete("/{coin}", status_code=204)
async def delete_coin(coin: str, request: Request) -> None:
    """Remove a coin from the registry.

    Guard: if a non-terminal FarbPosition exists for this coin → 409 Conflict.
    Deleting a coin with an open position would leave the engine with no
    spec for the position's coin (leverage, maint_ratio would be missing).
    Force-close the position first, then delete.
    """
    coin = coin.upper()
    repo = _get_repo(request)

    entry = await repo.get(coin)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Coin {coin!r} not found in registry")

    has_open = await repo.has_open_position(coin)
    if has_open:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete {coin!r}: a non-terminal FarbPosition exists for this coin. "
                "Force-close the position first, then delete."
            ),
        )

    await repo.delete(coin)
    await _reload_registry(request)
    # 204 No Content — return None
