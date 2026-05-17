"""add instance_token to strategies

Revision ID: b3f9a1c2e845
Revises: 79a1ac103d56
Create Date: 2026-05-17 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f9a1c2e845'
down_revision: Union[str, None] = '79a1ac103d56'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.add_column(sa.Column("instance_token", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("strategies", schema=None) as batch_op:
        batch_op.drop_column("instance_token")
