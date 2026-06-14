"""add xsmom tables

Revision ID: 024a443e02d8
Revises: 3f8a1b9c4d2e
Create Date: 2026-06-14 21:23:19.759361

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '024a443e02d8'
down_revision: Union[str, None] = '3f8a1b9c4d2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('xsmom_daily_prices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('coin', sa.String(), nullable=False),
    sa.Column('day_ms', sa.Integer(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('coin', 'day_ms')
    )
    with op.batch_alter_table('xsmom_daily_prices', schema=None) as batch_op:
        batch_op.create_index('ix_xsmom_daily_prices_coin_day', ['coin', 'day_ms'], unique=False)

    op.create_table('xsmom_positions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=False),
    sa.Column('coin', sa.String(), nullable=False),
    sa.Column('side', sa.Enum('LONG', 'SHORT', 'NONE', name='side', native_enum=False, length=8), nullable=False),
    sa.Column('state', sa.Enum('NEW', 'OPENED', 'CLOSE', 'CLOSED', 'FAILED', name='xsmomstate', native_enum=False, length=12), nullable=False),
    sa.Column('state_data', sa.JSON(), nullable=False),
    sa.Column('perp_position_id', sa.Integer(), nullable=True),
    sa.Column('collateral_position_id', sa.Integer(), nullable=True),
    sa.Column('target_qty', sa.Float(), nullable=True),
    sa.Column('opened_at', sa.Integer(), nullable=False),
    sa.Column('closed_at', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['collateral_position_id'], ['positions.id'], name='fk_xsmom_positions_collateral_position_id', use_alter=True),
    sa.ForeignKeyConstraint(['perp_position_id'], ['positions.id'], name='fk_xsmom_positions_perp_position_id', use_alter=True),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('xsmom_positions', schema=None) as batch_op:
        batch_op.create_index('ix_xsmom_positions_state', ['state'], unique=False)
        batch_op.create_index('ix_xsmom_positions_strategy', ['strategy_id'], unique=False)

    op.create_table('xsmom_scans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('ranking_json', sa.JSON(), nullable=False),
    sa.Column('n_long', sa.Integer(), nullable=False),
    sa.Column('n_short', sa.Integer(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('xsmom_scans', schema=None) as batch_op:
        batch_op.create_index('ix_xsmom_scans_strategy_ts', ['strategy_id', 'ts_ms'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('xsmom_scans', schema=None) as batch_op:
        batch_op.drop_index('ix_xsmom_scans_strategy_ts')

    op.drop_table('xsmom_scans')
    with op.batch_alter_table('xsmom_positions', schema=None) as batch_op:
        batch_op.drop_index('ix_xsmom_positions_strategy')
        batch_op.drop_index('ix_xsmom_positions_state')

    op.drop_table('xsmom_positions')
    with op.batch_alter_table('xsmom_daily_prices', schema=None) as batch_op:
        batch_op.drop_index('ix_xsmom_daily_prices_coin_day')

    op.drop_table('xsmom_daily_prices')
