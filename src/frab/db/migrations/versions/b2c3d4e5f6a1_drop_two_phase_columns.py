"""drop two-phase-dynamic dedicated columns

Revision ID: b2c3d4e5f6a1
Revises: a1b2c3d4e5f6
Create Date: 2026-05-28 00:00:00.000000

Drops position_min_hold_hours and consec_negative_hours from the positions
table.  These values are now stored in Position.state JSON under the same
key names (F1.4e migration).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2c3d4e5f6a1"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_column("position_min_hold_hours")
        batch_op.drop_column("consec_negative_hours")


def downgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "consec_negative_hours",
                sa.Integer(),
                nullable=True,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "position_min_hold_hours",
                sa.Integer(),
                nullable=True,
                server_default="0",
            )
        )
