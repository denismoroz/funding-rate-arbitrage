"""Point-in-time coin selection for the re-tuned trend variant.

Two rules, both mirroring q-lab's `qlab.strategies.live.trend` (`_top_k_by_market_cap_mask`,
`qlab.data.snapshot._top_k_by_volume_mask`):

* top-K by MARKET CAP, stablecoins excluded. Market caps come from CoinGecko's keyless
  `/coins/markets` (the live source); q-lab used CoinMarketCap weekly snapshots. The two sources can
  disagree on ranks and on symbols (KNOWN DIFFERENCE, see README.md, «Trend с недельной перенастройкой»). The ranking is
  refreshed once per ISO week, like q-lab's weekly snapshots, and held for the week; the history gate is
  applied every day. The PAST weeks (the backfill, `cap_history.py`) use q-lab's CoinMarketCap snapshots
  instead; the first live bar switches to CoinGecko and each week's source is recorded.
* top-K by traded VOLUME: trailing 24h USD volume of Hyperliquid's own daily candles, taken from the
  candle BEFORE the last closed one (q-lab: `rolling(1 bar).sum().shift(1)`, "strictly before the bar").
  Fully point-in-time: it is a pure function of candles that already existed.

Both are restricted to Hyperliquid perps that are listed now, tradeable at the bar (a candle closed at
it) and, with `top_k_requires_history`, holding at least `min_history_days` daily closes at the bar.
Ties go by coin name (q-lab: column order, `rank(method="first")`).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

DAY_MS = 86_400_000

# How venues list a low-priced coin per many units: Hyperliquid kPEPE, ... (q-lab coinmarketcap._UNIT_PREFIXES).
UNIT_PREFIXES = ("k", "1000", "1000000", "1M")

# Used on top of CoinGecko's own `stablecoins` category, so a failed category call cannot let one through.
STABLECOIN_SYMBOLS = frozenset({
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USDD", "PYUSD", "USDS", "USD1", "FRAX", "LUSD",
    "GUSD", "USDP", "RLUSD", "USDG", "USDTB", "BUIDL", "USDY", "USDX", "SUSDE", "SUSDS", "EURC",
})

SNAPSHOT_KEEP = 80          # ranked HL-mapped coins kept in a snapshot (25 + room for the history gate)


def week_key(decision_ms: int) -> str:
    """ISO week of the last CLOSED daily candle's open date.

    q-lab labels a daily bar by its candle's OPEN time and cuts its weeks (W-SUN) on that label, so the first
    bar of a week is the one that includes the Monday candle: live, the 00:00 UTC rebalance of Tuesday.
    """
    d = datetime.fromtimestamp((decision_ms - DAY_MS) / 1000, tz=timezone.utc).isocalendar()
    return f"{d.year}-W{d.week:02d}"


def venue_symbol(symbol: str, names: set[str]) -> str | None:
    """HL perp name for a market-data symbol: the plain symbol, else the same coin listed per many units
    (q-lab coinmarketcap.venue_symbol)."""
    if symbol in names:
        return symbol
    for prefix in UNIT_PREFIXES:
        if f"{prefix}{symbol}" in names:
            return f"{prefix}{symbol}"
    return None


class CoinGeckoClient:
    """Keyless CoinGecko public API: market caps and the stablecoin category. An optional demo key is read
    from FRAB_COINGECKO_KEY (header x-cg-demo-api-key); none is needed."""

    def __init__(self, base_url: str = "https://api.coingecko.com/api/v3", *,
                 client: httpx.AsyncClient | None = None, retries: int = 3, backoff_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._http = client or httpx.AsyncClient(timeout=30.0)
        self._own = client is None
        self._retries = retries
        self._backoff = backoff_s

    async def aclose(self) -> None:
        if self._own:
            await self._http.aclose()

    async def _get(self, path: str, params: dict) -> list[dict]:
        headers = {"accept": "application/json"}
        key = os.environ.get("FRAB_COINGECKO_KEY")
        if key:
            headers["x-cg-demo-api-key"] = key
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                resp = await self._http.get(f"{self._base}{path}", params=params, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt + 1 < self._retries:
                    await asyncio.sleep(self._backoff * (attempt + 1))
        raise RuntimeError(f"coingecko {path} failed: {type(last).__name__}") from last

    async def markets(self, *, per_page: int = 250, category: str | None = None) -> list[dict]:
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page, "page": 1,
                  "sparkline": "false"}
        if category:
            params["category"] = category
        return await self._get("/coins/markets", params)


def build_cap_snapshot(markets: list[dict], stable_ids: set[str], hl_names: set[str], *, taken_ms: int,
                       week: str, rank_limit: int = 200) -> dict:
    """The week's market-cap ranking restricted to HL perps, stablecoins excluded.

    Mirrors q-lab: only the `rank_limit` largest coins exist in a snapshot; a symbol is resolved to the
    HIGHEST-ranked coin carrying it (deduplication happens before the exclusion, as in `as_of_frame`);
    then coins tagged stablecoin are dropped; coins with no HL perp are not ranked. `unmapped` lists the
    large coins that found no HL perp, for audit.
    """
    rows = [m for m in markets if m.get("market_cap") and m.get("symbol")]
    rows.sort(key=lambda m: (m.get("market_cap_rank") or 10**9, -float(m["market_cap"])))
    rows = [m for m in rows if (m.get("market_cap_rank") or 10**9) <= rank_limit]
    seen: set[str] = set()
    ranked: list[list] = []
    unmapped: list[str] = []
    excluded: list[str] = []
    for m in rows:
        sym = str(m["symbol"]).upper()
        hl = venue_symbol(sym, hl_names)
        if hl is None:
            if len(unmapped) < 60:
                unmapped.append(sym)
            continue
        if hl in seen:
            continue
        seen.add(hl)
        if m.get("id") in stable_ids or sym in STABLECOIN_SYMBOLS:
            excluded.append(hl)
            continue
        ranked.append([hl, float(m["market_cap"]), m.get("market_cap_rank")])
    taken = datetime.fromtimestamp(taken_ms / 1000, tz=timezone.utc).date().isoformat()
    return {"source": "coingecko", "taken_ms": taken_ms, "snapshot_date": taken, "week": week,
            "rank_limit": rank_limit,
            "ranked": ranked[:SNAPSHOT_KEEP], "unmapped": unmapped, "excluded_stable": excluded}


async def fetch_cap_snapshot(client: CoinGeckoClient, hl_names: set[str], *, taken_ms: int, week: str,
                             rank_limit: int = 200) -> dict:
    markets = await client.markets(per_page=250)
    try:
        stable_ids = {str(m["id"]) for m in await client.markets(per_page=250, category="stablecoins")}
    except RuntimeError:
        logger.warning("coingecko stablecoin category unavailable; using the static symbol list only")
        stable_ids = set()
    return build_cap_snapshot(markets, stable_ids, hl_names, taken_ms=taken_ms, week=week, rank_limit=rank_limit)


def select_universes(*, cap_ranked: list[str], closes_count: dict[str, int], tradeable: set[str],
                     volume_usd: dict[str, float], k_cap: int, k_volume: int, min_history: int | None
                     ) -> dict[str, list[str]]:
    """{rule: coins} for one bar. `closes_count` = daily closes held at the bar, `tradeable` = coins with a
    candle closed at it, `volume_usd` = trailing 24h USD volume as of the candle before the last closed one."""
    def eligible(c: str) -> bool:
        return c in tradeable and (min_history is None or closes_count.get(c, 0) >= min_history)

    cap = [c for c in cap_ranked if eligible(c)][:k_cap]
    vol = sorted((c for c in volume_usd if eligible(c) and volume_usd[c] == volume_usd[c]),
                 key=lambda c: (-volume_usd[c], c))[:k_volume]
    return {"cap": cap, "volume": vol}

