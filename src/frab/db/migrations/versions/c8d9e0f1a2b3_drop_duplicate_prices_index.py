"""drop ix_prices_lookup: it duplicates the unique constraint's index

UNIQUE (exchange_id, coin, ts_ms) on prices already builds an index on exactly
those columns; ix_prices_lookup repeated it and took ~155 MB on prod (2026-10).

Revision ID: c8d9e0f1a2b3
Revises: d4e5f6a7b8c9
Create Date: 2026-10-25
"""
from __future__ import annotations

from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_prices_lookup", table_name="prices")


def downgrade() -> None:
    op.create_index("ix_prices_lookup", "prices", ["exchange_id", "coin", "ts_ms"], unique=False)
