"""backfill_open_to_pre_post_breakeven

Revision ID: 3f8a1b9c4d2e
Revises: eb3c2d4580b2
Create Date: 2026-06-10

For every farb_positions row with state='OPEN', determine whether the
position's cumulative funding has EVER reached or exceeded total_fees_paid.

The phase latch is one-way: a position is POST_BREAKEVEN if its running
cumulative funding (summed from funding_accruals in chronological order, per
the position's PERP leg) ever hit >= total_fees_paid at any point in history.
We use the running maximum of the cumulative sum — NOT the current snapshot
value in state_data — because funding can go negative and the snapshot can
dip back below the break-even threshold after the latch should have fired.

Linkage:
  farb_positions.id
    → positions.farb_position_id  (PERP leg)
      → funding_accruals.position_id

SQLAlchemy stores FarbState enum NAMEs (uppercase) as VARCHAR:
  FarbState.PRE_BREAKEVEN  → 'PRE_BREAKEVEN'
  FarbState.POST_BREAKEVEN → 'POST_BREAKEVEN'
  (old removed state)      → 'OPEN'

downgrade() maps both PRE/POST back to 'OPEN'.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f8a1b9c4d2e'
down_revision: str = 'eb3c2d4580b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Fetch all OPEN farb positions with their total_fees_paid from state_data JSON.
    # SQLite's json_extract handles the $.total_fees_paid path; the value is a
    # REAL that may be NULL if the JSON key is absent (treat NULL as 0.0).
    open_positions = conn.execute(sa.text("""
        SELECT
            id,
            COALESCE(CAST(json_extract(state_data, '$.total_fees_paid') AS REAL), 0.0)
                AS total_fees_paid
        FROM farb_positions
        WHERE state = 'OPEN'
    """)).fetchall()

    if not open_positions:
        return

    for row in open_positions:
        fp_id = row[0]
        total_fees_paid = row[1]

        # Compute the running cumulative sum of funding accruals for this
        # farb_position, ordered by timestamp, via the PERP positions leg.
        # The running max of that cumulative tells us whether the position
        # ever crossed the break-even threshold.
        #
        # We need the per-row running sum; SQLite supports window functions
        # from version 3.25 (2018). We use a subquery approach that is safe
        # on any SQLite version via a correlated SUM (O(n²) but accrual counts
        # per position are small — typically a few hundred rows).
        #
        # Approach: sum all accruals up to and including each row, then take MAX.
        result = conn.execute(sa.text("""
            SELECT COALESCE(MAX(running_cum), 0.0)
            FROM (
                SELECT
                    fa.id,
                    fa.ts_ms,
                    (
                        SELECT COALESCE(SUM(fa2.amount), 0.0)
                        FROM funding_accruals fa2
                        JOIN positions p2 ON p2.id = fa2.position_id
                        WHERE p2.farb_position_id = :fp_id
                          AND (fa2.ts_ms < fa.ts_ms
                               OR (fa2.ts_ms = fa.ts_ms AND fa2.id <= fa.id))
                    ) AS running_cum
                FROM funding_accruals fa
                JOIN positions p ON p.id = fa.position_id
                WHERE p.farb_position_id = :fp_id
            ) AS t
        """), {"fp_id": fp_id}).scalar()

        peak_cum = result if result is not None else 0.0

        if peak_cum >= total_fees_paid:
            new_state = 'POST_BREAKEVEN'
        else:
            new_state = 'PRE_BREAKEVEN'

        conn.execute(sa.text("""
            UPDATE farb_positions
            SET state = :new_state
            WHERE id = :fp_id
        """), {"new_state": new_state, "fp_id": fp_id})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE farb_positions
        SET state = 'OPEN'
        WHERE state IN ('PRE_BREAKEVEN', 'POST_BREAKEVEN')
    """))
