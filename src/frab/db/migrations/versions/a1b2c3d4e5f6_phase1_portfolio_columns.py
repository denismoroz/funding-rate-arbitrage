"""phase1 portfolio columns

Revision ID: a1b2c3d4e5f6
Revises: c8e6a2d4f9b1
Create Date: 2026-05-27 12:00:00.000000

Adds exchange, state, notional_usd, margin_reserve_usd to the positions table.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "c8e6a2d4f9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "exchange",
                sa.String(),
                nullable=False,
                server_default="hyperliquid",
            )
        )
        batch_op.add_column(
            sa.Column(
                "state",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )
        batch_op.add_column(
            sa.Column(
                "notional_usd",
                sa.Float(),
                nullable=True,
                server_default="0.0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "margin_reserve_usd",
                sa.Float(),
                nullable=True,
                server_default="0.0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_column("margin_reserve_usd")
        batch_op.drop_column("notional_usd")
        batch_op.drop_column("state")
        batch_op.drop_column("exchange")
