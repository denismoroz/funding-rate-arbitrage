"""Strategy B v2 paper engine tables: b2_books, b2_events, b2_equity.

Revision ID: a9b2c3d4e5f6
Revises: f1a2b3c4d5e6
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a9b2c3d4e5f6"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "b2_books",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("coin", sa.String(), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("last_bar_ms", sa.Integer(), nullable=True),
        sa.Column("updated_at_ms", sa.Integer(), nullable=False),
        sa.UniqueConstraint("strategy_id", "coin"),
    )
    op.create_table(
        "b2_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts_ms", sa.Integer(), nullable=False),
        sa.Column("coin", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=True),
    )
    op.create_index("ix_b2_events_strategy_ts", "b2_events", ["strategy_id", "ts_ms"])
    op.create_table(
        "b2_equity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts_ms", sa.Integer(), nullable=False),
        sa.Column("coin", sa.String(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("book_equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("spot_value", sa.Float(), nullable=False),
        sa.Column("short_pnl", sa.Float(), nullable=False),
        sa.Column("carry_cash", sa.Float(), nullable=False),
        sa.Column("hedge_on", sa.Boolean(), nullable=False),
        sa.Column("carry_on", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("strategy_id", "coin", "ts_ms"),
    )
    op.create_index("ix_b2_equity_strategy_ts", "b2_equity", ["strategy_id", "ts_ms"])


def downgrade() -> None:
    op.drop_index("ix_b2_equity_strategy_ts", table_name="b2_equity")
    op.drop_table("b2_equity")
    op.drop_index("ix_b2_events_strategy_ts", table_name="b2_events")
    op.drop_table("b2_events")
    op.drop_table("b2_books")
