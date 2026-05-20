"""add wallet_snapshots table

Revision ID: a92f7b3e1d44
Revises: f4c1d9e2a7b3
Create Date: 2026-05-20 06:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a92f7b3e1d44'
down_revision: Union[str, None] = 'f4c1d9e2a7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'wallet_snapshots',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
        sa.Column('account_value', sa.Float(), nullable=False),
        sa.Column('perp_equity', sa.Float(), nullable=False),
        sa.Column('spot_equity', sa.Float(), nullable=False),
        sa.Column('withdrawable', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('wallet_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_wallet_snapshots_ts', ['ts'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('wallet_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_wallet_snapshots_ts')
    op.drop_table('wallet_snapshots')
