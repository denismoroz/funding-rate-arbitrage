"""Server wiring and the committed deploy files of the re-tuned trend books (T52b)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from frab.db.models import Strategy as StrategyRow
from frab.db.session import session_scope
from frab.server import _find_trend_retune_strategies
from frab.strategy.trend.cap_history import DEFAULT_PATH, REPO_ROOT
from frab.strategy.trend.retune_params import RetuneParams

DEPLOY = REPO_ROOT / "deploy"


async def _add(sf, name: str, params: dict) -> int:
    async with session_scope(sf) as s:
        row = StrategyRow(name=name, version="v1-paper", params_json=params, status="active")
        s.add(row)
        await s.flush()
        return row.id


@pytest.mark.asyncio
async def test_one_engine_row_per_strategy_whose_name_starts_with_trend_retune(session_factory):
    await _add(session_factory, "trend", {"coins": ["BTC"]})                  # the hand-listed book: not ours
    await _add(session_factory, "xsmom", {})
    ids = [await _add(session_factory, n, json.loads((DEPLOY / f).read_text()))
           for n, f in (("trend_retune_3k", "trend_retune_3k_params.json"),
                        ("trend_retune_10k", "trend_retune_10k_params.json"))]
    await _add(session_factory, "retune_trend", {})                           # a prefix, not a substring
    found = await _find_trend_retune_strategies(session_factory)
    assert [(n, i) for n, i, _ in found] == [("trend_retune_3k", ids[0]), ("trend_retune_10k", ids[1])]
    assert [p.trend.capital_usd for _, _, p in found] == [3_000.0, 10_000.0]


@pytest.mark.asyncio
async def test_a_bad_row_fails_loudly_naming_the_strategy(session_factory):
    await _add(session_factory, "trend_retune_bad", {"lookback_sets": {"only": [30]}, "universe_rules": ["cap"]})
    with pytest.raises(ValueError, match="trend_retune_bad"):
        await _find_trend_retune_strategies(session_factory)


@pytest.mark.asyncio
async def test_no_rows_means_no_engines(session_factory):
    assert await _find_trend_retune_strategies(session_factory) == []


def test_the_two_params_files_differ_only_in_capital_and_both_backfill():
    small = json.loads((DEPLOY / "trend_retune_3k_params.json").read_text())
    big = json.loads((DEPLOY / "trend_retune_10k_params.json").read_text())
    assert small.pop("capital_usd") == 3_000.0 and big.pop("capital_usd") == 10_000.0
    assert small == big
    p = RetuneParams.from_dict(small)
    assert p.backfill and p.backfill_start == "2025-01-01" and p.trend.rebalance_hour_utc == 0
    assert (REPO_ROOT / p.cap_history_path) == DEFAULT_PATH and DEFAULT_PATH.is_file()
    # everything but the backfill and the capital is the validated spec's: same grid, same rules, same costs
    spec = RetuneParams.from_dict(json.loads((DEPLOY / "trend_retune_params.json").read_text()))
    assert RetuneParams.from_dict(small).candidates() == spec.candidates()
    assert (p.top_k_by_market_cap, p.top_k_by_volume, p.cost_rate, p.trend.min_history_days) == (
        spec.top_k_by_market_cap, spec.top_k_by_volume, spec.cost_rate, spec.trend.min_history_days)


def test_the_sql_in_the_readme_inserts_exactly_the_params_files():
    """The README's inline JSON is what gets pasted into sqlite3; it must equal the files."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for name, fname in (("trend_retune_3k", "trend_retune_3k_params.json"),
                        ("trend_retune_10k", "trend_retune_10k_params.json")):
        expected = json.dumps(json.loads((DEPLOY / fname).read_text()), separators=(",", ":"))
        assert f"'{name}', 'v1-paper', '{expected}', 'active'" in text, name


def test_the_exported_json_is_the_one_the_loader_reads():
    assert isinstance(DEFAULT_PATH, Path) and DEFAULT_PATH.parent == DEPLOY
