"""coin_registry table + seed

Revision ID: 294489218bcb
Revises: c7e9f1a2b3d4
Create Date: 2026-06-17 11:13:42.204532

Seeds exactly 7 rows from the verified ground-truth constants:
  RESEARCH_LEVERAGE / RESEARCH_MAINT_RATIO / MAINNET_SPOT_TOKEN_MAP
Active universe (5): BTC, ETH, SOL, HYPE, PURR
Known but not deployed (2): ZEC, XPL  (active=false)
All 7 have bridge_safe=true and a known spot_token.
validated_at is set to the migration epoch-ms (they are live de-facto).
"""
from __future__ import annotations

import time
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '294489218bcb'
down_revision: Union[str, None] = 'c7e9f1a2b3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── Seed data (ground-truth; DO NOT edit without matching constants.py audit) ──
# Source: RESEARCH_LEVERAGE / RESEARCH_MAINT_RATIO / MAINNET_SPOT_TOKEN_MAP
_SEED_ROWS: list[dict] = [
    # coin         leverage  maint_ratio  position_size_usd  active  spot_token  sz_decimals  bridge_safe
    {"coin": "BTC",  "leverage": 40, "maint_ratio": 0.010, "position_size_usd": None, "active": True,  "spot_token": "UBTC",  "sz_decimals": None, "bridge_safe": True},
    {"coin": "ETH",  "leverage": 25, "maint_ratio": 0.010, "position_size_usd": None, "active": True,  "spot_token": "UETH",  "sz_decimals": None, "bridge_safe": True},
    {"coin": "HYPE", "leverage": 10, "maint_ratio": 0.025, "position_size_usd": None, "active": True,  "spot_token": "HYPE",  "sz_decimals": None, "bridge_safe": True},
    {"coin": "PURR", "leverage":  3, "maint_ratio": 0.025, "position_size_usd": None, "active": True,  "spot_token": "PURR",  "sz_decimals": None, "bridge_safe": True},
    {"coin": "SOL",  "leverage": 20, "maint_ratio": 0.025, "position_size_usd": None, "active": True,  "spot_token": "USOL",  "sz_decimals": None, "bridge_safe": True},
    {"coin": "XPL",  "leverage": 10, "maint_ratio": 0.025, "position_size_usd": None, "active": False, "spot_token": "XPL",   "sz_decimals": None, "bridge_safe": True},
    {"coin": "ZEC",  "leverage": 10, "maint_ratio": 0.025, "position_size_usd": None, "active": False, "spot_token": "ZEC",   "sz_decimals": None, "bridge_safe": True},
]


def upgrade() -> None:
    # Capture the migration timestamp once so all seeded rows share the same value.
    now_ms: int = int(time.time() * 1000)

    op.create_table(
        'coin_registry',
        sa.Column('coin', sa.String(), nullable=False),
        sa.Column('leverage', sa.Integer(), nullable=False),
        sa.Column('maint_ratio', sa.Float(), nullable=False),
        sa.Column('position_size_usd', sa.Float(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('spot_token', sa.String(), nullable=True),
        sa.Column('sz_decimals', sa.Integer(), nullable=True),
        sa.Column('bridge_safe', sa.Boolean(), nullable=False),
        sa.Column('validated_at', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('coin'),
    )
    with op.batch_alter_table('coin_registry', schema=None) as batch_op:
        batch_op.create_index('ix_coin_registry_active', ['active'], unique=False)

    # Seed the 7 known rows with validated_at = now_ms.
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
        [{**row, "validated_at": now_ms} for row in _SEED_ROWS],
    )


def downgrade() -> None:
    with op.batch_alter_table('coin_registry', schema=None) as batch_op:
        batch_op.drop_index('ix_coin_registry_active')

    op.drop_table('coin_registry')
