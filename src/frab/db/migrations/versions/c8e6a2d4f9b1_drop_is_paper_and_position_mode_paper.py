"""drop is_paper column and convert position mode paper to live

Revision ID: c8e6a2d4f9b1
Revises: a92f7b3e1d44
Create Date: 2026-05-21 12:00:00.000000

Note: downgrade() restores the is_paper column (defaulting all rows to 0/False),
but cannot reverse the mode='paper' → 'live' data conversion.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e6a2d4f9b1'
down_revision: Union[str, None] = 'a92f7b3e1d44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert any legacy paper positions to live
    op.execute("UPDATE positions SET mode = 'live' WHERE mode = 'paper'")

    # Drop the is_paper column from fills
    with op.batch_alter_table('fills', schema=None) as batch_op:
        batch_op.drop_column('is_paper')


def downgrade() -> None:
    # Restore is_paper column (all rows default to False/0; paper data is unrecoverable)
    with op.batch_alter_table('fills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_paper', sa.Boolean(), nullable=False,
                                      server_default=sa.text('0')))
