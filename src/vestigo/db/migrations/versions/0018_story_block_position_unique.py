"""unique (story_id, position) on story_blocks

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-26

Document order in a forensic report must not be ambiguous. Gap positions were
derived from a plain read of the sibling set, so two concurrent inserts could
land on the same position and the blocks would then come back in whatever
order the engine picked — differently between polls. The store now serializes
position mutations on the story row; this index is the invariant behind that,
and doubles as the covering index for the ``WHERE story_id = ? ORDER BY
position`` read that every story page issues.

Existing rows are renumbered onto the canonical stride first, since a database
written by the racy code may already hold duplicates.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

#: Keep in sync with ``vestigo.db.postgres.STORY_POSITION_GAP``. Duplicated
#: rather than imported: a migration has to describe the schema as it was at
#: this revision, not follow later application changes.
POSITION_GAP = 1024


def upgrade() -> None:
    bind = op.get_bind()
    blocks = sa.table(
        "story_blocks",
        sa.column("id", sa.String),
        sa.column("story_id", sa.String),
        sa.column("position", sa.Integer),
    )
    rows = bind.execute(
        sa.select(blocks.c.id, blocks.c.story_id, blocks.c.position).order_by(
            blocks.c.story_id, blocks.c.position, blocks.c.id
        )
    ).all()

    by_story: dict[str, list[str]] = {}
    for row in rows:
        by_story.setdefault(row.story_id, []).append(row.id)

    # Park every row at a distinct negative rank before writing the finals, so
    # the rewrite never transiently collides with a row still holding a target
    # position (the unique index below would reject it).
    for rank, row in enumerate(rows, start=1):
        bind.execute(blocks.update().where(blocks.c.id == row.id).values(position=-rank))
    for ordered_ids in by_story.values():
        for index, block_id in enumerate(ordered_ids, start=1):
            bind.execute(
                blocks.update().where(blocks.c.id == block_id).values(position=index * POSITION_GAP)
            )

    op.create_index(
        "ix_story_blocks_story_position",
        "story_blocks",
        ["story_id", "position"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_story_blocks_story_position", table_name="story_blocks")
