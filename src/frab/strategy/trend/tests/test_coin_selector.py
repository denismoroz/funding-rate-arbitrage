"""Coin selection: market-cap ranking (stablecoins out, HL-only, symbol mapping) and point-in-time volume."""
from __future__ import annotations

import httpx
import pytest

from frab.strategy.trend.coin_selector import (
    CoinGeckoClient, build_cap_snapshot, fetch_cap_snapshot, select_universes, venue_symbol)
from frab.strategy.trend.retune import build_day
from frab.strategy.trend.tests.test_retune import DAY_MS, T0

HL = {"BTC", "ETH", "SOL", "kPEPE", "DOGE", "HYPE", "LINK"}


def row(id_, sym, cap, rank):
    return {"id": id_, "symbol": sym, "market_cap": cap, "market_cap_rank": rank}


MARKETS = [
    row("bitcoin", "btc", 2_000e9, 1), row("ethereum", "eth", 400e9, 2), row("tether", "usdt", 150e9, 3),
    row("usd-coin", "usdc", 80e9, 4), row("solana", "sol", 70e9, 5), row("wrapped-bitcoin", "wbtc", 60e9, 6),
    row("pepe", "pepe", 5e9, 7), row("fake-sol", "sol", 1e9, 8), row("hyperliquid", "hype", 3e9, 9),
    row("never-listed", "zzz", 2e9, 10), row("dogecoin", "doge", 4e9, 250),
]


def test_venue_symbol_maps_unit_prefixed_hl_names():
    assert venue_symbol("PEPE", HL) == "kPEPE" and venue_symbol("BTC", HL) == "BTC"
    assert venue_symbol("ZZZ", HL) is None


def test_cap_snapshot_excludes_stablecoins_keeps_only_hl_perps_and_ranks_by_cap():
    snap = build_cap_snapshot(MARKETS, {"tether", "usd-coin"}, HL, taken_ms=1, week="2026-W44", rank_limit=200)
    coins = [r[0] for r in snap["ranked"]]
    assert coins == ["BTC", "ETH", "SOL", "kPEPE", "HYPE"]        # no USDT/USDC, no WBTC/ZZZ (no HL perp)
    assert "DOGE" not in coins                                    # rank 250 is outside the top 200
    assert [r[1] for r in snap["ranked"]] == sorted((r[1] for r in snap["ranked"]), reverse=True)
    assert "WBTC" in snap["unmapped"] and "ZZZ" in snap["unmapped"]


def test_a_symbol_is_resolved_to_the_highest_ranked_coin_carrying_it():
    snap = build_cap_snapshot(MARKETS, set(), HL, taken_ms=1, week="w")
    sol = [r for r in snap["ranked"] if r[0] == "SOL"]
    assert len(sol) == 1 and sol[0][1] == 70e9


def test_stablecoin_symbol_list_backs_up_a_failed_category_call():
    markets = [row("some-usd", "usde", 9e9, 1), row("bitcoin", "btc", 1e9, 2)]
    hl = HL | {"USDE"}                                             # even if an HL perp existed
    snap = build_cap_snapshot(markets, set(), hl, taken_ms=1, week="w")
    assert [r[0] for r in snap["ranked"]] == ["BTC"] and snap["excluded_stable"] == ["USDE"]


def _series(vol_by_coin, days=12, last_vol_boost=None):
    out = {}
    for c, v in vol_by_coin.items():
        by = {T0 + d * DAY_MS: (10.0, v) for d in range(days)}
        if last_vol_boost:
            by[T0 + (days - 1) * DAY_MS] = (10.0, v * last_vol_boost.get(c, 1))
        out[c] = by
    return out


def test_volume_rule_uses_the_candle_before_the_last_closed_one_and_never_later_candles():
    series = _series({"A": 5.0, "B": 3.0, "C": 9.0, "D": 1.0})
    bar = T0 + 10 * DAY_MS                         # last closed candle opens at day 9; the one before is day 8
    series["D"][T0 + 8 * DAY_MS] = (10.0, 1000.0)  # D was the busiest on the candle that counts
    series["A"][T0 + 9 * DAY_MS] = (10.0, 1e9)     # a huge volume on the last closed candle must NOT count yet
    series["B"][T0 + 10 * DAY_MS] = (10.0, 1e12)   # nor on the candle still open at the bar
    day = build_day(series, {}, bar, None)
    u = select_universes(cap_ranked=[], closes_count={c: 10 for c in series}, tradeable=set(day.price_now),
                         volume_usd=day.volume_usd, k_cap=2, k_volume=2, min_history=5)
    assert u["volume"] == ["D", "C"]


def test_history_gate_tradeable_gate_and_stable_tie_order():
    vol = {"A": 100.0, "B": 100.0, "C": 50.0, "NEW": 1e6}
    u = select_universes(cap_ranked=["NEW", "C", "A", "GONE", "B"],
                         closes_count={"A": 200, "B": 200, "C": 200, "NEW": 30, "GONE": 200},
                         tradeable={"A", "B", "C", "NEW"}, volume_usd=vol, k_cap=3, k_volume=2, min_history=151)
    assert u["cap"] == ["C", "A", "B"]             # NEW too young, GONE has no candle at the bar
    assert u["volume"] == ["A", "B"]               # tie A/B by name; NEW's huge volume is gated out
    no_gate = select_universes(cap_ranked=["NEW", "C"], closes_count={"NEW": 30, "C": 200},
                               tradeable={"NEW", "C"}, volume_usd=vol, k_cap=1, k_volume=1, min_history=None)
    assert no_gate["cap"] == ["NEW"] and no_gate["volume"] == ["NEW"]


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        cat = request.url.params.get("category")
        body = [{"id": "tether"}, {"id": "usd-coin"}] if cat == "stablecoins" else MARKETS
        return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_coingecko_client_uses_the_keyless_markets_endpoint_and_the_stablecoin_category():
    tr = _Transport()
    async with httpx.AsyncClient(transport=tr) as http:
        cg = CoinGeckoClient("https://example.test/api/v3", client=http)
        snap = await fetch_cap_snapshot(cg, HL, taken_ms=5, week="2026-W44")
    assert [r[0] for r in snap["ranked"]][:3] == ["BTC", "ETH", "SOL"]
    assert {c.url.path for c in tr.calls} == {"/api/v3/coins/markets"}
    assert any(c.url.params.get("category") == "stablecoins" for c in tr.calls)
    assert all("x-cg-demo-api-key" not in c.headers for c in tr.calls)
