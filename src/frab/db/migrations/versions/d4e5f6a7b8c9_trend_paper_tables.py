"""Trend paper engine tables: trend_books, trend_events, trend_equity.

Revision ID: d4e5f6a7b8c9
Revises: a9b2c3d4e5f6
Create Date: 2026-09-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "a9b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trend_books",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("state_json", sa.JSON(), nullable=False),
        sa.Column("last_bar_ms", sa.Integer(), nullable=True),
        sa.Column("updated_at_ms", sa.Integer(), nullable=False),
        sa.UniqueConstraint("strategy_id"),
    )
    op.create_table(
        "trend_events",
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
    op.create_index("ix_trend_events_strategy_ts", "trend_events", ["strategy_id", "ts_ms"])
    op.create_table(
        "trend_equity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ts_ms", sa.Integer(), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("cash", sa.Float(), nullable=False),
        sa.Column("unrealized", sa.Float(), nullable=False),
        sa.Column("gross_notional", sa.Float(), nullable=False),
        sa.Column("net_notional", sa.Float(), nullable=False),
        sa.Column("legs", sa.Integer(), nullable=False),
        sa.Column("funding_total", sa.Float(), nullable=False),
        sa.Column("fees", sa.Float(), nullable=False),
        sa.UniqueConstraint("strategy_id", "ts_ms"),
    )
    op.create_index("ix_trend_equity_strategy_ts", "trend_equity", ["strategy_id", "ts_ms"])


def downgrade() -> None:
    op.drop_index("ix_trend_equity_strategy_ts", table_name="trend_equity")
    op.drop_table("trend_equity")
    op.drop_index("ix_trend_events_strategy_ts", table_name="trend_events")
    op.drop_table("trend_events")
    op.drop_table("trend_books")
