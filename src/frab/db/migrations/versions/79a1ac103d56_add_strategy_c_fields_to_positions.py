"""add strategy_c fields to positions

Revision ID: 79a1ac103d56
Revises: dc38c9a572da
Create Date: 2026-05-17 07:39:32.919223

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '79a1ac103d56'
down_revision: Union[str, None] = 'dc38c9a572da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("position_min_hold_hours", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("consec_negative_hours", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("positions", schema=None) as batch_op:
        batch_op.drop_column("consec_negative_hours")
        batch_op.drop_column("position_min_hold_hours")
