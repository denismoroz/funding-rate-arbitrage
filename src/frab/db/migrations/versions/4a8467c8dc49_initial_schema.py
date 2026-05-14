"""initial schema

Revision ID: 4a8467c8dc49
Revises:
Create Date: 2026-05-14 11:12:51.040061

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4a8467c8dc49'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('level', sa.String(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('message', sa.String(), nullable=False),
    sa.Column('payload_json', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index('ix_events_level', ['level'], unique=False)
        batch_op.create_index('ix_events_time', ['ts_ms'], unique=False)

    op.create_table('exchanges',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('funding_interval_h', sa.Integer(), nullable=False),
    sa.Column('spot_taker_bps', sa.Float(), nullable=False),
    sa.Column('perp_taker_bps', sa.Float(), nullable=False),
    sa.Column('created_at_ms', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )

    op.create_table('strategies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('version', sa.String(), nullable=False),
    sa.Column('params_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('started_at_ms', sa.Integer(), nullable=True),
    sa.Column('stopped_at_ms', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'version')
    )

    op.create_table('markets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('exchange_id', sa.Integer(), nullable=False),
    sa.Column('coin', sa.String(), nullable=False),
    sa.Column('has_spot', sa.Boolean(), nullable=False),
    sa.Column('has_perp', sa.Boolean(), nullable=False),
    sa.Column('min_size', sa.Float(), nullable=False),
    sa.Column('tick_size', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['exchange_id'], ['exchanges.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('exchange_id', 'coin')
    )

    op.create_table('equity_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('total_equity', sa.Float(), nullable=False),
    sa.Column('cash', sa.Float(), nullable=False),
    sa.Column('spot_value', sa.Float(), nullable=False),
    sa.Column('perp_unrealized', sa.Float(), nullable=False),
    sa.Column('perp_realized_cum', sa.Float(), nullable=False),
    sa.Column('funding_cum', sa.Float(), nullable=False),
    sa.Column('fees_cum', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('equity_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_equity_lookup', ['strategy_id', 'ts_ms'], unique=False)

    op.create_table('funding_rates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('rate', sa.Float(), nullable=False),
    sa.Column('premium', sa.Float(), nullable=True),
    sa.Column('annualized_pct', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('market_id', 'ts_ms')
    )
    with op.batch_alter_table('funding_rates', schema=None) as batch_op:
        batch_op.create_index('ix_funding_rates_lookup', ['market_id', 'ts_ms'], unique=False)

    op.create_table('positions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('mode', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('opened_at_ms', sa.Integer(), nullable=False),
    sa.Column('closed_at_ms', sa.Integer(), nullable=True),
    sa.Column('spot_units', sa.Float(), nullable=False),
    sa.Column('perp_units', sa.Float(), nullable=False),
    sa.Column('entry_spot_price', sa.Float(), nullable=False),
    sa.Column('entry_perp_price', sa.Float(), nullable=False),
    sa.Column('exit_spot_price', sa.Float(), nullable=True),
    sa.Column('exit_perp_price', sa.Float(), nullable=True),
    sa.Column('realized_pnl', sa.Float(), nullable=False),
    sa.Column('funding_collected', sa.Float(), nullable=False),
    sa.Column('fees_paid', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.create_index('ix_positions_market_time', ['market_id', 'opened_at_ms'], unique=False)
        batch_op.create_index('ix_positions_status', ['strategy_id', 'status'], unique=False)

    op.create_table('prices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('mark', sa.Float(), nullable=False),
    sa.Column('spot', sa.Float(), nullable=True),
    sa.Column('bid', sa.Float(), nullable=True),
    sa.Column('ask', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('market_id', 'ts_ms')
    )
    with op.batch_alter_table('prices', schema=None) as batch_op:
        batch_op.create_index('ix_prices_lookup', ['market_id', 'ts_ms'], unique=False)

    op.create_table('signals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('strategy_id', sa.Integer(), nullable=False),
    sa.Column('market_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('signal_value', sa.Float(), nullable=False),
    sa.Column('regime_pass', sa.Boolean(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['strategy_id'], ['strategies.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['market_id'], ['markets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('strategy_id', 'market_id', 'ts_ms')
    )

    op.create_table('fills',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('ts_ms', sa.Integer(), nullable=False),
    sa.Column('leg', sa.String(), nullable=False),
    sa.Column('side', sa.String(), nullable=False),
    sa.Column('qty', sa.Float(), nullable=False),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('fee', sa.Float(), nullable=False),
    sa.Column('slippage_bps', sa.Float(), nullable=False),
    sa.Column('is_paper', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('fills')
    op.drop_table('signals')
    with op.batch_alter_table('prices', schema=None) as batch_op:
        batch_op.drop_index('ix_prices_lookup')

    op.drop_table('prices')
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_index('ix_positions_status')
        batch_op.drop_index('ix_positions_market_time')

    op.drop_table('positions')
    with op.batch_alter_table('funding_rates', schema=None) as batch_op:
        batch_op.drop_index('ix_funding_rates_lookup')

    op.drop_table('funding_rates')
    op.drop_table('markets')
    with op.batch_alter_table('equity_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_equity_lookup')

    op.drop_table('equity_snapshots')
    op.drop_table('strategies')
    op.drop_table('exchanges')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_time')
        batch_op.drop_index('ix_events_level')

    op.drop_table('events')
