"""add position status states and fill client_ref

Revision ID: f4c1d9e2a7b3
Revises: b3f9a1c2e845
Create Date: 2026-05-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4c1d9e2a7b3'
down_revision: Union[str, None] = 'b3f9a1c2e845'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("fills", schema=None) as batch_op:
        batch_op.add_column(sa.Column("client_ref", sa.String(), nullable=True))
        batch_op.create_unique_constraint("uq_fills_client_ref", ["client_ref"])


def downgrade() -> None:
    with op.batch_alter_table("fills", schema=None) as batch_op:
        batch_op.drop_constraint("uq_fills_client_ref", type_="unique")
        batch_op.drop_column("client_ref")
