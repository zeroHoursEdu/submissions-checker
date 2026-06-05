"""Add semesters table with pre-seeded academic calendar rows.

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-05

Spring semesters: Feb 1 – Jun 30
Fall semesters:   Sep 1 – Jan 31 (following year)
Covers Spring 2026 through Fall 2035 (20 rows).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | None = None
depends_on: str | None = None

_SEMESTERS = [
    # (name, season, start_date, end_date)
    ("Spring 2026", "SPRING", "2026-02-01", "2026-06-30"),
    ("Fall 2026",   "FALL",   "2026-09-01", "2027-01-31"),
    ("Spring 2027", "SPRING", "2027-02-01", "2027-06-30"),
    ("Fall 2027",   "FALL",   "2027-09-01", "2028-01-31"),
    ("Spring 2028", "SPRING", "2028-02-01", "2028-06-30"),
    ("Fall 2028",   "FALL",   "2028-09-01", "2029-01-31"),
    ("Spring 2029", "SPRING", "2029-02-01", "2029-06-30"),
    ("Fall 2029",   "FALL",   "2029-09-01", "2030-01-31"),
    ("Spring 2030", "SPRING", "2030-02-01", "2030-06-30"),
    ("Fall 2030",   "FALL",   "2030-09-01", "2031-01-31"),
    ("Spring 2031", "SPRING", "2031-02-01", "2031-06-30"),
    ("Fall 2031",   "FALL",   "2031-09-01", "2032-01-31"),
    ("Spring 2032", "SPRING", "2032-02-01", "2032-06-30"),
    ("Fall 2032",   "FALL",   "2032-09-01", "2033-01-31"),
    ("Spring 2033", "SPRING", "2033-02-01", "2033-06-30"),
    ("Fall 2033",   "FALL",   "2033-09-01", "2034-01-31"),
    ("Spring 2034", "SPRING", "2034-02-01", "2034-06-30"),
    ("Fall 2034",   "FALL",   "2034-09-01", "2035-01-31"),
    ("Spring 2035", "SPRING", "2035-02-01", "2035-06-30"),
    ("Fall 2035",   "FALL",   "2035-09-01", "2036-01-31"),
]


def upgrade() -> None:
    semesters_table = op.create_table(
        "semesters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("season", sa.String(10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_semesters_dates", "semesters", ["start_date", "end_date"])

    op.bulk_insert(
        semesters_table,
        [
            {"name": name, "season": season, "start_date": start, "end_date": end}
            for name, season, start, end in _SEMESTERS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_semesters_dates", table_name="semesters")
    op.drop_table("semesters")
