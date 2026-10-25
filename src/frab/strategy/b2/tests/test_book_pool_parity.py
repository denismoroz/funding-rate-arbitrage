"""The production books are untouched by the shared margin pool and the delayed top-up.

`fixtures/golden_book_digests.json` was recorded from the book BEFORE `shared_margin` and
`topup_delay_hours` existed, by replaying stored hourly histories (BTC, SOL, DOGE, 8000 bars each,
`fixtures/*.csv.gz`) through `CoinBook` with the engine's own loop (`_replay.replay`) under the
production settings and under stressed ones that reach top-ups, liquidations and the carry
sleeve. Every bar's fills, full book state and equity go into the digest, so one changed bit fails.
The new params at their defaults must reproduce each digest exactly -- stated explicitly, not
just left to the defaults, since that is what a production row without the keys loads as.
"""
from __future__ import annotations

import json

import pytest

from frab.strategy.b2.book import CoinBook
from frab.strategy.b2.params import B2Params
from frab.strategy.b2.tests._replay import FIXTURES, VARIANTS, load_history, replay, variant_params

GOLDEN = json.loads((FIXTURES / "golden_book_digests.json").read_text())
SHAPES = json.loads((FIXTURES / "golden_shapes.json").read_text())

# frab.db strategies.params_json of the production book `b2`, read 2026-10-02 (the file
# q-lab/seed/prod_params/2026-10-02-b2.json), and `b2_cold`, which differs in the headroom.
PROD_B2 = {
    "coins": ["BTC", "ETH", "SOL", "AVAX"], "capital_usd": 257.0, "spot_share": 0.5,
    "hedge_threshold": 0.0, "sticky_exit_hours": 12, "ratchet_threshold": 0.5, "carry_enabled": False,
    "carry_fraction": 0.6, "carry_entry_apr": 0.1, "carry_exit_hours": 24, "slippage": 0.0005,
    "min_order_usd": 10.0, "margin_enabled": True,
    "short_leverage": {"BTC": 3.0, "ETH": 2.0, "SOL": 1.5, "AVAX": 1.5}, "default_leverage": 1.0,
    "maint_margin_rate": {"BTC": 0.0125, "ETH": 0.02, "SOL": 0.025, "AVAX": 0.05},
    "default_maint_margin_rate": 0.05, "margin_buffer": 0.1, "hedge_margin_headroom": 1.0,
    "rebalance_at_im_share": 0.5, "mode": "paper",
}


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_stored_history_replays_to_the_recorded_digest(key):
    coin, name = key.split("/")
    got = replay(coin, variant_params(coin, name), load_history(coin))
    assert got == GOLDEN[key]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_new_params_stated_at_their_defaults_change_nothing(key):
    coin, name = key.split("/")
    params = B2Params(coins=(coin,), capital_usd=64.25, shared_margin=False, topup_delay_hours=0,
                      **VARIANTS[name])
    assert replay(coin, params, load_history(coin)) == GOLDEN[key]


def test_the_replays_cover_what_the_parity_claims_to_cover():
    kinds = {k for g in GOLDEN.values() for k in g["kinds"]}
    assert {"hedge_open", "hedge_close", "ratchet_sell", "margin_rebalance", "liquidation",
            "carry_open", "carry_reduce", "refill_buy"} <= kinds


def test_book_state_and_params_have_the_shapes_they_had():
    assert sorted(CoinBook.new("BTC", B2Params()).to_state()) == SHAPES["state_keys"]
    assert B2Params().to_dict() == SHAPES["params_to_dict"]
    old_fields = set(SHAPES["params_fields"])
    assert set(B2Params.__dataclass_fields__) - old_fields == {"shared_margin", "topup_delay_hours"}


def test_production_param_rows_load_and_store_back_unchanged():
    for row in (PROD_B2, {**PROD_B2, "hedge_margin_headroom": 1.5}):
        assert B2Params.from_dict(dict(row)).to_dict() == row   # no new key is written for a default


def test_new_params_are_written_only_when_set():
    on = B2Params.from_dict(PROD_B2 | {"shared_margin": True, "topup_delay_hours": 6})
    stored = on.to_dict()
    assert stored["shared_margin"] is True and stored["topup_delay_hours"] == 6
    assert B2Params.from_dict(stored) == on


def test_a_pending_top_up_is_stored_and_loaded_but_absent_when_none():
    book = CoinBook.new("BTC", B2Params(coins=("BTC",)))
    assert "topup_due_ms" not in book.to_state()
    book.topup_due_ms = 123
    assert CoinBook.from_state(book.to_state()).topup_due_ms == 123
    old_row = {k: v for k, v in CoinBook.new("BTC", B2Params(coins=("BTC",))).to_state().items()}
    assert CoinBook.from_state(old_row).topup_due_ms is None


@pytest.mark.parametrize("bad, match", [
    ({"topup_delay_hours": -1}, "whole number"),
    ({"topup_delay_hours": 1.5}, "whole number"),
    ({"topup_delay_hours": 3, "margin_enabled": False}, "needs margin_enabled"),
    ({"shared_margin": True, "margin_enabled": False}, "needs margin_enabled"),
    ({"shared_margin": True, "carry_enabled": True}, "carry"),
])
def test_unsupported_combinations_are_refused(bad, match):
    with pytest.raises(ValueError, match=match):
        B2Params.from_dict(PROD_B2 | bad)
