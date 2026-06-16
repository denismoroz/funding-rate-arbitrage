"""add account column to wallet_snapshots (per-account cash scoping)

Two strategies (FRAB + XSMOM) run as two distinct HL accounts but both resolve
exchange_id by name "hyperliquid" → both write wallet_snapshots under the same
exchange_id with no account discriminator. Ledger._compute_cash picks the latest
row per (exchange_id, coin), so whichever engine wrote last wins → one strategy's
cash transiently absorbs the other wallet's USDC (the equity "bumps").

This adds an ``account`` column (the HL account address, lower-cased) so a
strategy-scoped Ledger can filter wallet_snapshots to its own wallet. Existing
rows get NULL account; the cash query only uses the *latest* row per group, and
every minute tick re-writes a fresh account-tagged snapshot, so it self-heals on
the first tick after deploy.

Revision ID: c7e9f1a2b3d4
Revises: b1c2d3e4f5a6
Create Date: 2026-06-16
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e9f1a2b3d4"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "wallet_snapshots",
        sa.Column("account", sa.String(), nullable=True),
    )
    # Supports the account-scoped latest-per-(coin) lookup in Ledger._compute_cash.
    op.create_index(
        "ix_wallet_snapshots_account_latest",
        "wallet_snapshots",
        ["account", "coin", "ts_ms"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wallet_snapshots_account_latest", table_name="wallet_snapshots"
    )
    op.drop_column("wallet_snapshots", "account")
