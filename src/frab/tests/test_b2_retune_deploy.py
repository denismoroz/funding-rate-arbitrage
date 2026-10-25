"""Server wiring (opt-in) and the committed deploy files of the monthly re-tuned Bv2 book."""
from __future__ import annotations

import json

import pytest

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.server import _B2_TESTS, _find_b2_retune_strategy
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.retune_engine import REPO_ROOT
from frab.strategy.b2.retune_history import HOUR_MS
from frab.strategy.b2.retune_params import B2RetuneParams

DEPLOY = REPO_ROOT / "deploy"


async def _add(sf, name: str, params: dict, status: str = "active") -> int:
    async with session_scope(sf) as s:
        row = StrategyRow(name=name, version="v2-paper-retune", params_json=params, status=status)
        s.add(row)
        await s.flush()
        return row.id


@pytest.mark.asyncio
async def test_the_engine_exists_only_if_a_row_named_b2_retune_was_added(session_factory):
    assert await _find_b2_retune_strategy(session_factory) is None          # the server never creates it
    await _add(session_factory, "b2", B2Params().to_dict())
    await _add(session_factory, "b2_cold", B2Params().to_dict())
    await _add(session_factory, "b2_retune_old", {})                         # a prefix is not the name
    assert await _find_b2_retune_strategy(session_factory) is None
    sid = await _add(session_factory, "b2_retune", json.loads((DEPLOY / "b2_retune_params.json").read_text()))
    found = await _find_b2_retune_strategy(session_factory)
    assert found is not None and found[0] == sid and found[1].candidates.keys() == {"b2", "b2_cold"}


@pytest.mark.asyncio
async def test_a_bad_row_fails_loudly_naming_the_strategy(session_factory):
    await _add(session_factory, "b2_retune", {"retune_candidates": {"only": {}}})
    with pytest.raises(ValueError, match="b2_retune"):
        await _find_b2_retune_strategy(session_factory)


def test_the_params_file_is_exactly_the_two_production_instances():
    """Candidates b2 / b2_cold are production's `_B2_TESTS` (server.py) differing only in the headroom."""
    p = B2RetuneParams.from_dict(json.loads((DEPLOY / "b2_retune_params.json").read_text()))
    prod = {name: params for name, _, params in _B2_TESTS}
    assert set(p.candidates) == set(prod)
    for name, params in prod.items():
        assert p.candidate_params(name) == params
    assert p.base.capital_usd == 257.0 and p.base.carry_enabled is False and p.refit_every == "M"
    assert list(p.candidates) == ["b2", "b2_cold"] and p.candidates["b2"] != p.candidates["b2_cold"]
    # the stand's start: 788 hours after the first bar of its panel; AVAX when UAVAX was listed
    assert p.start_ms("BTC") == p.history_start_ms + 788 * HOUR_MS == p.start_ms("ETH") == p.start_ms("SOL")
    assert p.coin_starts["AVAX"] == "2026-05-06T14:00:00Z"


def test_the_seed_covers_the_history_from_its_start_without_holes():
    p = B2RetuneParams.from_dict(json.loads((DEPLOY / "b2_retune_params.json").read_text()))
    seed = json.loads((REPO_ROOT / p.history_seed_path).read_text())
    assert seed["origin_ms"] == p.history_start_ms
    n = (seed["last_open_ms"] - seed["origin_ms"]) // HOUR_MS + 1
    assert set(seed["coins"]) == set(p.coins)
    for coin, cols in seed["coins"].items():
        assert len(cols["close"]) == len(cols["high"]) == len(cols["funding"]) == n
        assert None not in cols["close"] and None not in cols["high"] and None not in cols["funding"]
        assert all(h >= c for h, c in zip(cols["high"], cols["close"]))


def test_the_sql_in_the_readme_inserts_exactly_the_params_file():
    """The README's inline JSON is what gets pasted into sqlite3; it must equal the file."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    expected = json.dumps(json.loads((DEPLOY / "b2_retune_params.json").read_text()), separators=(",", ":"))
    assert f"('b2_retune', 'v2-paper-retune', '{expected}', 'active')" in text
