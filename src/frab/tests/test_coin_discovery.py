"""Phase C: CoinDiscovery service tests — fixtures only, NO network.

Covers:
- Normal coin with spot pair (BTC → UBTC): discovers facts, writes row with validated_at.
- Perp-only coin (no USDC spot pair): spot_token=None, still registers with validated_at.
- Bridge-token coin (resolves to AVAX0/LINK0/AAVE0): guard fires; spot_token NOT written.
- Perp not in meta: discover() raises ValueError, no row written.
- Round-trip: validate_and_register → CoinRegistry.reload() → universe() excludes
  while active=False, includes after set_active(True).
- Price-parity guard: spot token with far-off price (UAVAX) → downgraded to perp-only.
- Price-parity fail-safe: missing/zero price → downgraded to perp-only.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from frab.coin_discovery import CoinDiscovery, DiscoveredFacts
from frab.coin_registry import CoinRegistry
from frab.db.session import init_db, make_session_factory
from frab.repo.coin_registry_repo import CoinRegistryRepo


# ── Fake HL client ────────────────────────────────────────────────────────────

def _make_perp_meta(coins: list[tuple[str, int]]) -> dict:
    """Build a fake HL meta response.

    coins: list of (name, szDecimals) tuples.
    """
    return {
        "universe": [
            {"name": name, "szDecimals": sz_dec}
            for name, sz_dec in coins
        ]
    }


def _make_spot_meta(
    pairs: list[tuple[str, str]],  # (base_token_name, pair_display_name) or raw tuples
) -> dict:
    """Build a fake HL spotMeta response.

    pairs: list of (base_token_name, pair_name) e.g. ("UBTC", "UBTC/USDC").
    USDC is always at token index 0.
    """
    tokens = [{"index": 0, "name": "USDC"}]
    universe = []
    for i, (base, pair_name) in enumerate(pairs, start=1):
        tokens.append({"index": i, "name": base})
        universe.append({
            "name": pair_name,
            "tokens": [i, 0],   # [base_idx, usdc_idx=0]
            "index": i,
        })
    return {"tokens": tokens, "universe": universe}


class FakeHLClient:
    """Minimal fake that replays pre-built HL API responses.

    Supports:
      - perp_meta()   → list[HLPerpMarketSpec]   (via the real HLClient parser)
      - spot_meta()   → HLSpotMeta               (via the real HLClient parser)
      - all_mids()    → dict[str, float]          (for price-parity check)
      - _post(body)   → raw dict                 (for validate_spot_pairs duck-typed call)

    ``all_mids_data``: dict keyed by coin ticker (perp) or "@<index>" (spot).
    When omitted, the fake synthesizes 1:1 parity prices (perp=100.0, spot=100.0
    via @<index>) so all standard tests pass the parity guard without change.
    """

    def __init__(
        self,
        *,
        perp_meta_raw: dict,
        spot_meta_raw: dict,
        all_mids_data: dict[str, float] | None = None,
    ) -> None:
        self._perp_meta_raw = perp_meta_raw
        self._spot_meta_raw = spot_meta_raw
        self._all_mids_data = all_mids_data

    async def all_mids(self) -> dict[str, float]:
        if self._all_mids_data is not None:
            return dict(self._all_mids_data)
        # Auto-generate 1:1 parity prices so existing tests need no explicit mids.
        # Perp coins: keyed by name at 100.0.
        # Spot pairs: keyed by "@<index>" at 100.5 (within 3% of 100.0).
        mids: dict[str, float] = {}
        for entry in self._perp_meta_raw.get("universe", []):
            mids[entry["name"]] = 100.0
        for entry in self._spot_meta_raw.get("universe", []):
            idx = entry.get("index")
            if isinstance(idx, int):
                mids[f"@{idx}"] = 100.5   # 0.5% above perp — within 3% tolerance
        return mids

    # validate_spot_pairs calls client._post({"type": "spotMeta"}) directly
    async def _post(self, body: dict) -> dict:
        if body.get("type") == "meta":
            return self._perp_meta_raw
        if body.get("type") == "spotMeta":
            return self._spot_meta_raw
        raise NotImplementedError(f"FakeHLClient._post: unexpected body {body!r}")

    # HLClient.perp_meta() parses {"type": "meta"} response
    async def perp_meta(self):
        from frab.exchanges.hyperliquid.wire import HLPerpMarketSpec
        return [
            HLPerpMarketSpec(
                name=entry["name"],
                sz_decimals=int(entry["szDecimals"]),
            )
            for entry in self._perp_meta_raw.get("universe", [])
        ]

    # HLClient.spot_meta() parses {"type": "spotMeta"} response
    async def spot_meta(self):
        from frab.exchanges.hyperliquid.client import HLClient
        # Reuse the real parser to avoid duplicating it
        _dummy = object.__new__(HLClient)
        # Build the same logic inline (the real method is a pure transformation):
        data = self._spot_meta_raw
        from frab.exchanges.hyperliquid.wire import HLSpotMeta, HLSpotPair
        token_by_idx: dict[int, str] = {}
        for t in data.get("tokens", []):
            tidx = t.get("index")
            tname = t.get("name", "")
            if isinstance(tidx, int) and tname:
                token_by_idx[tidx] = tname

        pairs: list[HLSpotPair] = []
        for entry in data.get("universe", []):
            idx = entry.get("index")
            if not isinstance(idx, int):
                continue
            raw_name = entry.get("name", "")
            if "/" in raw_name:
                pairs.append(HLSpotPair(index=idx, name=raw_name))
                continue
            toks = entry.get("tokens") or []
            if len(toks) >= 2 and isinstance(toks[0], int) and isinstance(toks[1], int):
                base = token_by_idx.get(toks[0])
                quote = token_by_idx.get(toks[1])
                if base and quote:
                    pairs.append(HLSpotPair(index=idx, name=f"{base}/{quote}"))
                    continue
            pairs.append(HLSpotPair(index=idx, name=raw_name))

        return HLSpotMeta(tokens=token_by_idx, pairs=pairs)


# ── DB fixtures ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def session_factory():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True, echo=False)
    await init_db(eng)
    sf = make_session_factory(eng)
    yield sf
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(session_factory):
    return CoinRegistryRepo(session_factory)


# ── HL fixture data ───────────────────────────────────────────────────────────

# Normal perp universe for most tests
_PERP_META = _make_perp_meta([
    ("BTC", 5),
    ("ETH", 4),
    ("SOL", 2),
    ("HYPE", 3),
    ("DOGE", 0),    # DOGE: perp exists, but no USDC spot pair on HL
    ("AVAXTEST", 2),  # fictitious coin whose spot resolves to a bridge token
])

# Spot pairs: normal coins + a bridge-token entry
_SPOT_META = _make_spot_meta([
    ("UBTC", "UBTC/USDC"),
    ("UETH", "UETH/USDC"),
    ("USOL", "USOL/USDC"),
    ("HYPE", "HYPE/USDC"),
    # Bridge token: AVAXTEST's spot token is AVAX0 (in BRIDGE_TOKEN_BLACKLIST)
    ("AVAX0", "AVAX0/USDC"),
    # No DOGE spot pair (perp-only)
])


# ── Test 1: Normal coin with wrapped spot token ───────────────────────────────

@pytest.mark.asyncio
async def test_discover_normal_coin_btc(repo):
    """BTC perp + UBTC/USDC spot → spot_token=UBTC, bridge_safe=True, sz_decimals=5."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("BTC")

    assert isinstance(facts, DiscoveredFacts)
    assert facts.coin == "BTC"
    assert facts.sz_decimals == 5
    assert facts.spot_token == "UBTC"
    assert facts.bridge_safe is True


@pytest.mark.asyncio
async def test_discover_identity_spot_token_hype(repo):
    """HYPE: base == coin (no U-prefix) → spot_token=HYPE, bridge_safe=True."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("HYPE")

    assert facts.coin == "HYPE"
    assert facts.spot_token == "HYPE"
    assert facts.bridge_safe is True


@pytest.mark.asyncio
async def test_validate_and_register_writes_row_with_validated_at(repo, session_factory):
    """validate_and_register for BTC writes a row with validated_at set."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    entry = await svc.validate_and_register(
        "BTC",
        leverage=40,
        maint_ratio=0.01,
        active=False,
    )

    assert entry.coin == "BTC"
    assert entry.spot_token == "UBTC"
    assert entry.sz_decimals == 5
    assert entry.bridge_safe is True
    assert entry.validated_at is not None, "validated_at must be set after registration"
    assert entry.active is False   # default: inactive until operator enables

    # Confirm it is persisted in the DB
    db_entry = await repo.get("BTC")
    assert db_entry is not None
    assert db_entry.validated_at is not None
    assert db_entry.spot_token == "UBTC"


# ── Test 2: Perp-only coin (no USDC spot pair) ───────────────────────────────

@pytest.mark.asyncio
async def test_discover_perp_only_coin(repo):
    """DOGE has no USDC spot pair → spot_token=None, bridge_safe=False."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("DOGE")

    assert facts.coin == "DOGE"
    assert facts.sz_decimals == 0
    assert facts.spot_token is None
    assert facts.bridge_safe is False


@pytest.mark.asyncio
async def test_validate_and_register_perp_only_sets_validated_at(repo, session_factory):
    """Perp-only coin (no spot) is still registerable; validated_at is written."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    entry = await svc.validate_and_register(
        "DOGE",
        leverage=5,
        maint_ratio=0.025,
        active=False,
    )

    assert entry.coin == "DOGE"
    assert entry.spot_token is None
    assert entry.bridge_safe is False
    assert entry.validated_at is not None, "perp-only coin must still get validated_at"

    db_entry = await repo.get("DOGE")
    assert db_entry is not None
    assert db_entry.validated_at is not None
    assert db_entry.spot_token is None


# ── Test 3: Bridge-token coin (spot resolves to blacklisted token) ───────────

@pytest.mark.asyncio
async def test_discover_bridge_token_sets_spot_token_none(repo):
    """AVAXTEST resolves AVAX0/USDC — a blacklisted bridge token.

    Expected: spot_token=None, bridge_safe=False (guard fires; perp-only treatment).
    The blacklisted token must NOT be stored in spot_token.
    """
    # AVAXTEST: perp exists; spot maps to AVAX0 (in BRIDGE_TOKEN_BLACKLIST)
    # In our _SPOT_META AVAX0 is listed, and AVAXTEST perp name would match
    # via the "U" + coin pattern only if "UAVAXTEST" were present.
    # The fixture uses "AVAXTEST" → the identity check matches "AVAXTEST" in base_to_spot.
    # Let's build a targeted spot meta that has AVAXTEST mapping to AVAX0 explicitly.
    # Strategy: coin="AVAXTEST", wrapped candidate="UAVAXTEST" (not in spot),
    # identity check: "AVAXTEST" in base_to_spot? No.
    # So we need a special fixture where the coin name IS the base token AND it's blacklisted.
    # Use coin="AVAX0" (hypothetical perp ticker that is itself a bridge token).

    # Alternatively: use AVAXTEST as identity where the spot base is named "AVAX0".
    # Our current discover() logic:
    #   coin in base_to_spot → identity match
    #   f"U{coin}" in base_to_spot → wrapped match
    # For AVAXTEST: "AVAXTEST" not in base_to_spot, "UAVAXTEST" not in base_to_spot → spot_token=None always.
    # That wouldn't test the guard.

    # To test the guard, use a coin whose name is in BRIDGE_TOKEN_BLACKLIST itself,
    # e.g. "AVAX0" as a perp ticker (hypothetical but valid for the unit test).

    perp_meta = _make_perp_meta([("AVAX0", 2), ("LINK0", 3), ("AAVE0", 2)])
    spot_meta = _make_spot_meta([
        ("AVAX0", "AVAX0/USDC"),
        ("LINK0", "LINK0/USDC"),
        ("AAVE0", "AAVE0/USDC"),
    ])

    client = FakeHLClient(perp_meta_raw=perp_meta, spot_meta_raw=spot_meta)
    svc = CoinDiscovery(client=client, repo=repo)

    # discover() for "AVAX0": identity match → "AVAX0" in base_to_spot → spot_token="AVAX0"
    # Then bridge guard: "AVAX0" in BRIDGE_TOKEN_BLACKLIST → spot_token=None, bridge_safe=False
    facts = await svc.discover("AVAX0")

    assert facts.spot_token is None, (
        "AVAX0 is in BRIDGE_TOKEN_BLACKLIST; spot_token must NOT be set"
    )
    assert facts.bridge_safe is False
    assert facts.sz_decimals == 2


@pytest.mark.asyncio
async def test_bridge_guard_fires_for_all_known_bridge_tokens(repo):
    """All three known bridge tokens trigger the guard when they appear as spot bases."""
    from frab.exchanges.hyperliquid.tokens import BRIDGE_TOKEN_BLACKLIST

    for bridge_tok in ("AVAX0", "LINK0", "AAVE0"):
        perp_meta = _make_perp_meta([(bridge_tok, 2)])
        spot_meta = _make_spot_meta([(bridge_tok, f"{bridge_tok}/USDC")])
        client = FakeHLClient(perp_meta_raw=perp_meta, spot_meta_raw=spot_meta)
        svc = CoinDiscovery(client=client, repo=repo)

        facts = await svc.discover(bridge_tok)

        assert facts.spot_token is None, (
            f"{bridge_tok!r} is a bridge token; spot_token must not be written"
        )
        assert facts.bridge_safe is False


@pytest.mark.asyncio
async def test_validate_and_register_bridge_token_writes_perp_only_row(repo):
    """Bridge-guard coin is registered as perp-only (spot_token=None, validated_at set)."""
    perp_meta = _make_perp_meta([("AVAX0", 2)])
    spot_meta = _make_spot_meta([("AVAX0", "AVAX0/USDC")])
    client = FakeHLClient(perp_meta_raw=perp_meta, spot_meta_raw=spot_meta)
    svc = CoinDiscovery(client=client, repo=repo)

    entry = await svc.validate_and_register(
        "AVAX0",
        leverage=3,
        maint_ratio=0.025,
        active=False,
    )

    assert entry.spot_token is None
    assert entry.bridge_safe is False
    assert entry.validated_at is not None

    # Confirm DB row has spot_token=None (never the bridge token string)
    db_entry = await repo.get("AVAX0")
    assert db_entry is not None
    assert db_entry.spot_token is None


# ── Test 4: Perp not in meta → discovery raises, no row written ──────────────

@pytest.mark.asyncio
async def test_discover_unknown_perp_raises(repo):
    """A coin not in HL perp meta raises ValueError; no row is written."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    with pytest.raises(ValueError, match="not found in HL perp meta"):
        await svc.discover("FAKECOIN")

    # No row must have been written
    entry = await repo.get("FAKECOIN")
    assert entry is None


@pytest.mark.asyncio
async def test_validate_and_register_unknown_perp_no_row_written(repo):
    """validate_and_register propagates the ValueError; no row is created."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    with pytest.raises(ValueError, match="not found in HL perp meta"):
        await svc.validate_and_register(
            "GHOST",
            leverage=5,
            maint_ratio=0.025,
            active=False,
        )

    entry = await repo.get("GHOST")
    assert entry is None


# ── Test 5: Round-trip with CoinRegistry ─────────────────────────────────────

@pytest.mark.asyncio
async def test_roundtrip_inactive_coin_excluded_from_universe(repo, session_factory):
    """After validate_and_register (active=False), reload() excludes the coin from universe()."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    await svc.validate_and_register("SOL", leverage=20, maint_ratio=0.025, active=False)

    registry = CoinRegistry(session_factory)
    await registry.reload()

    # active=False → not in universe
    assert "SOL" not in registry.universe()
    # But coin IS in the registry (spec accessible)
    spec = registry.get_coin_spec("SOL")
    assert spec.leverage == 20


@pytest.mark.asyncio
async def test_roundtrip_activating_coin_includes_in_universe(repo, session_factory):
    """set_active(True) then reload() → coin appears in universe() (validated_at is set)."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    # Register SOL as inactive
    await svc.validate_and_register("SOL", leverage=20, maint_ratio=0.025, active=False)

    registry = CoinRegistry(session_factory)
    await registry.reload()
    assert "SOL" not in registry.universe()

    # Activate it
    await repo.set_active("SOL", True)

    await registry.reload()
    assert "SOL" in registry.universe()


@pytest.mark.asyncio
async def test_roundtrip_validated_at_none_always_excluded(repo, session_factory):
    """A coin inserted directly with validated_at=None is NEVER in universe() even if active=True.

    This is the Phase B invariant; we confirm it still holds after the discovery write-path.
    """
    # Insert without going through discover (simulates a corrupt/partial row)
    await repo.upsert(
        "UNVALIDATED",
        leverage=5,
        maint_ratio=0.025,
        position_size_usd=None,
        active=True,           # active=True but...
        spot_token=None,
        sz_decimals=None,
        bridge_safe=False,
        validated_at=None,     # ...no validated_at → must NOT be tradeable
    )

    registry = CoinRegistry(session_factory)
    await registry.load()

    assert "UNVALIDATED" not in registry.universe()


@pytest.mark.asyncio
async def test_roundtrip_spot_token_map_after_registration(repo, session_factory):
    """After registering BTC + SOL, spot_token_map() reflects both."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    await svc.validate_and_register("BTC", leverage=40, maint_ratio=0.01, active=True)
    await svc.validate_and_register("SOL", leverage=20, maint_ratio=0.025, active=True)

    registry = CoinRegistry(session_factory)
    await registry.load()

    spot_map = registry.spot_token_map()
    assert spot_map["BTC"] == "UBTC"
    assert spot_map["SOL"] == "USOL"


# ── Test 6: Ticker case-normalization ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_normalizes_ticker_case(repo):
    """discover() accepts lowercase/mixed-case input and normalizes to uppercase."""
    client = FakeHLClient(perp_meta_raw=_PERP_META, spot_meta_raw=_SPOT_META)
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("btc")
    assert facts.coin == "BTC"

    facts2 = await svc.discover("Eth")
    assert facts2.coin == "ETH"


# ── Test 7: Price-parity guard ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_discover_rejects_spot_token_with_far_off_price(repo):
    """AVAX: resolves UAVAX/USDC spot pair (not blacklisted), but spot mid ($8)
    is far from perp mid ($13.5) — ~41% deviation >> 3% tolerance.

    Expected: spot_token=None, bridge_safe=False (parity guard fires).
    This is the exact UAVAX footgun documented in tokens.py.
    """
    # AVAX perp + UAVAX spot (UAVAX is NOT in BRIDGE_TOKEN_BLACKLIST)
    perp_meta = _make_perp_meta([("AVAX", 2)])
    spot_meta = _make_spot_meta([("UAVAX", "UAVAX/USDC")])

    # UAVAX spot pair has index=1 in _make_spot_meta (starts at 1).
    # Perp mid = 13.5, spot mid = 8.0 → deviation ~41% >> 3%.
    all_mids = {
        "AVAX": 13.5,   # perp mid, keyed by canonical ticker
        "@1": 8.0,      # spot mid, keyed by @<pair_index>
    }

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("AVAX")

    assert facts.spot_token is None, (
        "UAVAX trades ~40% below the AVAX perp: parity guard must reject it "
        "(spot_token must be None)"
    )
    assert facts.bridge_safe is False, (
        "bridge_safe must be False when parity guard fires"
    )
    assert facts.sz_decimals == 2, "sz_decimals must still be populated from perp meta"


@pytest.mark.asyncio
async def test_validate_and_register_uavax_writes_perp_only_row(repo):
    """validate_and_register for AVAX (UAVAX parity failure) writes perp-only row."""
    perp_meta = _make_perp_meta([("AVAX", 2)])
    spot_meta = _make_spot_meta([("UAVAX", "UAVAX/USDC")])
    all_mids = {"AVAX": 13.5, "@1": 8.0}

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    entry = await svc.validate_and_register(
        "AVAX",
        leverage=3,
        maint_ratio=0.025,
        active=False,
    )

    assert entry.spot_token is None
    assert entry.bridge_safe is False
    assert entry.validated_at is not None, "perp-only row must still have validated_at"

    db_entry = await repo.get("AVAX")
    assert db_entry is not None
    assert db_entry.spot_token is None
    assert db_entry.bridge_safe is False


@pytest.mark.asyncio
async def test_discover_missing_perp_price_fails_safe(repo):
    """If the perp price is missing from allMids, parity cannot be verified.

    Fail-safe: treat as perp-only (spot_token=None, bridge_safe=False).
    Never register a spot leg whose parity we could not check.
    """
    perp_meta = _make_perp_meta([("BTC", 5)])
    spot_meta = _make_spot_meta([("UBTC", "UBTC/USDC")])

    # BTC perp price is absent from allMids (e.g. transient gap)
    all_mids = {"@1": 100.5}   # spot present, perp missing

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("BTC")

    assert facts.spot_token is None, (
        "Missing perp price must trigger fail-safe: spot_token=None"
    )
    assert facts.bridge_safe is False


@pytest.mark.asyncio
async def test_discover_missing_spot_price_fails_safe(repo):
    """If the spot pair price is missing from allMids, parity cannot be verified.

    Fail-safe: treat as perp-only (spot_token=None, bridge_safe=False).
    """
    perp_meta = _make_perp_meta([("BTC", 5)])
    spot_meta = _make_spot_meta([("UBTC", "UBTC/USDC")])

    # Spot price is absent from allMids (no @1 entry, no symbolic key)
    all_mids = {"BTC": 100.0}   # perp present, spot missing

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("BTC")

    assert facts.spot_token is None, (
        "Missing spot price must trigger fail-safe: spot_token=None"
    )
    assert facts.bridge_safe is False


@pytest.mark.asyncio
async def test_discover_parity_passes_for_within_tolerance(repo):
    """A spot price within 3% of the perp price is accepted (bridge_safe=True).

    BTC perp=100.0, spot=102.5 → deviation=2.5% < 3% → passes.
    """
    perp_meta = _make_perp_meta([("BTC", 5)])
    spot_meta = _make_spot_meta([("UBTC", "UBTC/USDC")])
    all_mids = {"BTC": 100.0, "@1": 102.5}   # 2.5% above — within tolerance

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("BTC")

    assert facts.spot_token == "UBTC"
    assert facts.bridge_safe is True


@pytest.mark.asyncio
async def test_discover_parity_fails_at_boundary(repo):
    """A spot price just over 3% tolerance is rejected (bridge_safe=False).

    BTC perp=100.0, spot=96.9 → deviation≈3.1% > 3% → rejected.
    """
    perp_meta = _make_perp_meta([("BTC", 5)])
    spot_meta = _make_spot_meta([("UBTC", "UBTC/USDC")])
    all_mids = {"BTC": 100.0, "@1": 96.9}   # 3.1% below — outside tolerance

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("BTC")

    assert facts.spot_token is None
    assert facts.bridge_safe is False


@pytest.mark.asyncio
async def test_discover_parity_uses_symbolic_key_fallback(repo):
    """For tokens whose spot pair is keyed by symbolic name (not @N) in allMids,
    the fallback "<spot_token>/USDC" lookup is used.

    This covers early HL pairs like PURR/USDC at @0.
    """
    perp_meta = _make_perp_meta([("PURR", 0)])
    spot_meta = _make_spot_meta([("PURR", "PURR/USDC")])

    # Index 1 for PURR (from _make_spot_meta), but allMids uses the symbolic key.
    # Omit "@1" to force the symbolic fallback path.
    all_mids = {
        "PURR": 0.14,          # perp mid (bare ticker)
        "PURR/USDC": 0.137,    # spot mid (symbolic pair name)
    }

    client = FakeHLClient(
        perp_meta_raw=perp_meta,
        spot_meta_raw=spot_meta,
        all_mids_data=all_mids,
    )
    svc = CoinDiscovery(client=client, repo=repo)

    facts = await svc.discover("PURR")

    # deviation = |0.137/0.14 - 1| ≈ 2.1% < 3% → should pass
    assert facts.spot_token == "PURR"
    assert facts.bridge_safe is True
