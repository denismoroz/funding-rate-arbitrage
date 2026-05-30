"""add_unique_farb_position_instrument

Revision ID: eb3c2d4580b2
Revises: 5965dec3c693
Create Date: 2026-05-30

Enforces "at most one Position per (farb_position_id, instrument)" — i.e. no
two SPOT/PERP/COLLATERAL legs on a single FarbPosition.

WARNING: If the existing DB has any (farb_position_id, instrument) duplicates,
this migration will fail with IntegrityError. The upgrade includes a pre-flight
check that raises a descriptive error before attempting to create the index.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eb3c2d4580b2'
down_revision = '5965dec3c693'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Pre-flight: detect duplicates so the failure message is human-readable
    conn = op.get_bind()
    dupes = conn.execute(sa.text("""
        SELECT farb_position_id, instrument, COUNT(*) as n
        FROM positions
        WHERE farb_position_id IS NOT NULL
        GROUP BY farb_position_id, instrument
        HAVING n > 1
    """)).fetchall()
    if dupes:
        raise RuntimeError(
            f"Cannot add UNIQUE(farb_position_id, instrument): "
            f"existing duplicates: {[dict(d._mapping) for d in dupes]}"
        )

    op.create_index(
        "uq_positions_farb_instrument",
        "positions",
        ["farb_position_id", "instrument"],
        unique=True,
        sqlite_where=sa.text("farb_position_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_positions_farb_instrument", table_name="positions")
