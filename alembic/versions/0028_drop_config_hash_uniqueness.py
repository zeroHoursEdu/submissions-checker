"""Drop the (subject_id, content_hash) uniqueness on subject_plugin_configs.

Revision ID: 0028
Revises: 0027

Config-apply now deduplicates against the latest version only, so rolling back
to an earlier archive inserts a new version carrying an already-seen hash.
"""

from __future__ import annotations

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_subject_plugin_configs_subject_hash", "subject_plugin_configs", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_subject_plugin_configs_subject_hash",
        "subject_plugin_configs",
        ["subject_id", "content_hash"],
    )
