"""add ix_wallet_snapshots_latest (exchange_id, coin, ts_ms)

Speeds up Ledger._compute_cash: finding the latest wallet_snapshot per
(exchange_id, coin) was an O(N^2) correlated NOT-EXISTS over the ever-growing
wallet_snapshots table (~48s on prod at 12k rows). The query is now a GROUP BY
MAX(ts_ms) which this index makes O(N).

Revision ID: b1c2d3e4f5a6
Revises: 024a443e02d8
Create Date: 2026-06-15
"""
from __future__ import annotations

from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "024a443e02d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_wallet_snapshots_latest",
        "wallet_snapshots",
        ["exchange_id", "coin", "ts_ms"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_wallet_snapshots_latest", table_name="wallet_snapshots")
