"""xsmom_coins_seed — add 31 XSMOM-universe coins to coin_registry

Revision ID: f1a2b3c4d5e6
Revises: 294489218bcb
Create Date: 2026-06-17

Seeds 31 coins from the DEFAULT_XSMOM_UNIVERSE that are NOT yet in
coin_registry (the 7 FRAB coins are already there).

Risk params (Phase F1 provenance invariant):
  leverage=3, maint_ratio=0.05
  These match FALLBACK_LEVERAGE / FALLBACK_MAINT_RATIO from constants.py.
  The old settings.get_coin_spec(coin) for any of these 31 coins
  returned exactly maint_ratio=0.05 (FALLBACK) because none appear in
  RESEARCH_MAINT_RATIO (only BTC/ETH/SOL/HYPE/ZEC/PURR/XPL do).

Market facts (discovered 2026-06-17 from HL public /info API):
  - All 31 coins confirmed present in HL perp meta (sz_decimals != NULL).
  - spot_token / bridge_safe from CoinDiscovery (bridge guard + parity
    guard applied; AVAX0/LINK0/AAVE0 never appear — UAVAX is not
    blacklisted).
  - WLD: UWLD spot found but parity failed (spot ~$1.75 vs perp ~$0.65,
    ~170% deviation) → perp-only (spot_token=None, bridge_safe=False).

active=False for all 31: XSMOM reads maint_ratio from registry but
manages its own universe via XsmomParams.universe.  These coins do NOT
enter the FRAB trading universe (active=False never appears in
registry.universe()).

downgrade() removes exactly the 31 coins by name.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = '294489218bcb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── Seed data (discovered 2026-06-17 from HL public /info API) ────────────────
# leverage=3 (FALLBACK_LEVERAGE), maint_ratio=0.05 (FALLBACK_MAINT_RATIO)
# active=False — perp-only for XSMOM watchdog; never in FRAB universe.
# validated_at = literal epoch-ms from discovery run (2026-06-17 ~UTC).
# Coins without a valid USDC spot pair (perp-only): spot_token=None, bridge_safe=False.
_VALIDATED_AT_MS: int = 1750157000000  # 2026-06-17T09:03:20Z (discovery run)

_XSMOM_ROWS: list[dict] = [
    # coin      sz_dec  spot_token   bridge_safe
    {"coin": "AAVE",   "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ADA",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "APT",    "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ARB",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "ATOM",   "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "AVAX",   "sz_decimals": 2, "spot_token": "UAVAX", "bridge_safe": True},
    {"coin": "BCH",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "BNB",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "CRV",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "DOGE",   "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "DOT",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "EIGEN",  "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "ENA",    "sz_decimals": 0, "spot_token": "UENA",  "bridge_safe": True},
    {"coin": "HMSTR",  "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "INJ",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "JTO",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "JUP",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "LINK",   "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "LTC",    "sz_decimals": 2, "spot_token": None,    "bridge_safe": False},
    {"coin": "NEAR",   "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "PENDLE", "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "PYTH",   "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "SUI",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "TAO",    "sz_decimals": 3, "spot_token": None,    "bridge_safe": False},
    {"coin": "TON",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "TRX",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "UNI",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "WLD",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
    {"coin": "XLM",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "XRP",    "sz_decimals": 0, "spot_token": None,    "bridge_safe": False},
    {"coin": "ZRO",    "sz_decimals": 1, "spot_token": None,    "bridge_safe": False},
]

# Names of new coins (used in downgrade to delete exactly these rows).
_XSMOM_COIN_NAMES: list[str] = [r["coin"] for r in _XSMOM_ROWS]


def upgrade() -> None:
    coin_registry = sa.table(
        'coin_registry',
        sa.column('coin', sa.String),
        sa.column('leverage', sa.Integer),
        sa.column('maint_ratio', sa.Float),
        sa.column('position_size_usd', sa.Float),
        sa.column('active', sa.Boolean),
        sa.column('spot_token', sa.String),
        sa.column('sz_decimals', sa.Integer),
        sa.column('bridge_safe', sa.Boolean),
        sa.column('validated_at', sa.Integer),
    )
    op.bulk_insert(
        coin_registry,
        [
            {
                "coin": row["coin"],
                "leverage": 3,
                "maint_ratio": 0.05,
                "position_size_usd": None,
                "active": False,
                "spot_token": row["spot_token"],
                "sz_decimals": row["sz_decimals"],
                "bridge_safe": row["bridge_safe"],
                "validated_at": _VALIDATED_AT_MS,
            }
            for row in _XSMOM_ROWS
        ],
    )


def downgrade() -> None:
    conn = op.get_bind()
    placeholders = ", ".join(f"'{c}'" for c in _XSMOM_COIN_NAMES)
    conn.execute(sa.text(f"DELETE FROM coin_registry WHERE coin IN ({placeholders})"))
