"""unified finding dispositions

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-09

Replaces the fragmented analyst-disposition mechanisms with the single
``finding_dispositions`` table (kinds: normal | dismissed | confirmed):

- ``detector_allowlist`` rows become value-scoped ``kind="normal"`` rows
  (1:1 column mapping) and the table is dropped.
- Per-event ``normal`` annotations become event-scoped ``kind="normal"``
  rows with ``detector="*"`` (a per-event normal excluded the event from
  every detector) and the annotation rows are deleted. This is destructive
  for the annotation rows themselves, but the verdicts survive as
  disposition rows and any audit-log entries for the original creations are
  untouched.
- ``pinned=True`` system annotations become event-scoped ``kind="confirmed"``
  rows carrying the annotation's detector and details snapshot; the
  annotation row itself is KEPT (it is the run-result record the UI reads) —
  only the "survives re-scans" intent moves to the disposition. The
  ``pinned`` column is then dropped.

Downgrade recreates the old structures and reverses the moves;
``dismissed`` rows (which have no legacy representation) are lost on
downgrade.
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _derived_id(source_id: str, kind: str) -> str:
    """Deterministic id for a migrated row, from the legacy row id + kind."""
    return f"disp_{kind}_{hashlib.sha256(f'{source_id}:{kind}'.encode()).hexdigest()[:16]}"


_dispositions = sa.table(
    "finding_dispositions",
    sa.column("id", sa.String),
    sa.column("case_id", sa.String),
    sa.column("timeline_id", sa.String),
    sa.column("kind", sa.String),
    sa.column("detector", sa.String),
    sa.column("field", sa.String),
    sa.column("value", sa.String),
    sa.column("source_id", sa.String),
    sa.column("event_id", sa.String),
    sa.column("note", sa.String),
    sa.column("details", sa.JSON),
    sa.column("created_by", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        "finding_dispositions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("timeline_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("detector", sa.String(length=32), server_default="*", nullable=False),
        sa.Column("field", sa.String(length=255), nullable=True),
        sa.Column("value", sa.String(length=4096), nullable=True),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("event_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=4096), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("case_id", "timeline_id", "source_id", "event_id"):
        op.create_index(
            op.f(f"ix_finding_dispositions_{col}"), "finding_dispositions", [col], unique=False
        )

    bind = op.get_bind()

    # 1. detector_allowlist rows -> value-scoped kind="normal".
    allowlist = sa.table(
        "detector_allowlist",
        sa.column("id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("timeline_id", sa.String),
        sa.column("detector", sa.String),
        sa.column("field", sa.String),
        sa.column("value", sa.String),
        sa.column("note", sa.String),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    allowlist_values = [
        {
            "id": _derived_id(row["id"], "normal"),
            "case_id": row["case_id"],
            "timeline_id": row["timeline_id"],
            "kind": "normal",
            "detector": row["detector"],
            "field": row["field"],
            "value": row["value"],
            "note": row["note"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        for row in bind.execute(sa.select(allowlist)).mappings()
    ]
    if allowlist_values:
        bind.execute(_dispositions.insert(), allowlist_values)

    annotations = sa.table(
        "annotations",
        sa.column("id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("source_id", sa.String),
        sa.column("event_id", sa.String),
        sa.column("annotation_type", sa.String),
        sa.column("content", sa.String),
        sa.column("origin", sa.String),
        sa.column("details", sa.JSON),
        sa.column("detector", sa.String),
        sa.column("pinned", sa.Boolean),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    # 2. Per-event "normal" annotations -> event-scoped kind="normal",
    #    detector="*" (the legacy exclusion applied to every detector).
    normal_values = [
        {
            "id": _derived_id(row["id"], "normal"),
            "case_id": row["case_id"],
            "kind": "normal",
            "detector": "*",
            "source_id": row["source_id"],
            "event_id": row["event_id"],
            "note": row["content"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        for row in bind.execute(
            sa.select(annotations).where(annotations.c.annotation_type == "normal")
        ).mappings()
    ]
    if normal_values:
        bind.execute(_dispositions.insert(), normal_values)
    bind.execute(sa.delete(annotations).where(annotations.c.annotation_type == "normal"))

    # 3. Pinned system annotations -> kind="confirmed"; annotation row kept.
    confirmed_values = [
        {
            "id": _derived_id(row["id"], "confirmed"),
            "case_id": row["case_id"],
            "kind": "confirmed",
            "detector": row["detector"] or "*",
            "source_id": row["source_id"],
            "event_id": row["event_id"],
            "details": row["details"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        for row in bind.execute(
            sa.select(annotations).where(annotations.c.pinned.is_(True))
        ).mappings()
    ]
    if confirmed_values:
        bind.execute(_dispositions.insert(), confirmed_values)

    with op.batch_alter_table("annotations") as batch_op:
        batch_op.drop_column("pinned")

    op.drop_index(op.f("ix_detector_allowlist_timeline_id"), table_name="detector_allowlist")
    op.drop_index(op.f("ix_detector_allowlist_case_id"), table_name="detector_allowlist")
    op.drop_table("detector_allowlist")


def downgrade() -> None:
    op.create_table(
        "detector_allowlist",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("timeline_id", sa.String(length=64), nullable=False),
        sa.Column("detector", sa.String(length=32), nullable=False),
        sa.Column("field", sa.String(length=255), nullable=False),
        sa.Column("value", sa.String(length=4096), nullable=False),
        sa.Column("note", sa.String(length=4096), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_detector_allowlist_case_id"), "detector_allowlist", ["case_id"], unique=False
    )
    op.create_index(
        op.f("ix_detector_allowlist_timeline_id"),
        "detector_allowlist",
        ["timeline_id"],
        unique=False,
    )
    with op.batch_alter_table("annotations") as batch_op:
        batch_op.add_column(
            sa.Column("pinned", sa.Boolean(), server_default="false", nullable=False)
        )

    bind = op.get_bind()
    allowlist = sa.table(
        "detector_allowlist",
        sa.column("id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("timeline_id", sa.String),
        sa.column("detector", sa.String),
        sa.column("field", sa.String),
        sa.column("value", sa.String),
        sa.column("note", sa.String),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    annotations = sa.table(
        "annotations",
        sa.column("id", sa.String),
        sa.column("case_id", sa.String),
        sa.column("source_id", sa.String),
        sa.column("event_id", sa.String),
        sa.column("annotation_type", sa.String),
        sa.column("content", sa.String),
        sa.column("origin", sa.String),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("pinned", sa.Boolean),
        sa.column("detector", sa.String),
    )
    rows = bind.execute(sa.select(_dispositions)).mappings().all()
    for row in rows:
        if row["kind"] == "normal" and row["field"] is not None:
            bind.execute(
                allowlist.insert().values(
                    id=row["id"],
                    case_id=row["case_id"],
                    timeline_id=row["timeline_id"],
                    detector=row["detector"],
                    field=row["field"],
                    value=row["value"],
                    note=row["note"],
                    created_by=row["created_by"],
                    created_at=row["created_at"],
                )
            )
        elif row["kind"] == "normal":
            bind.execute(
                annotations.insert().values(
                    id=row["id"],
                    case_id=row["case_id"],
                    source_id=row["source_id"],
                    event_id=row["event_id"],
                    annotation_type="normal",
                    content=row["note"] or "normal operation",
                    origin="user",
                    created_by=row["created_by"],
                    created_at=row["created_at"],
                    pinned=False,
                )
            )
        elif row["kind"] == "confirmed":
            bind.execute(
                sa.update(annotations)
                .where(
                    annotations.c.case_id == row["case_id"],
                    annotations.c.event_id == row["event_id"],
                    annotations.c.annotation_type == "anomaly",
                    annotations.c.detector == row["detector"],
                )
                .values(pinned=True)
            )
        # kind="dismissed" has no legacy representation — dropped.

    op.drop_index(op.f("ix_finding_dispositions_event_id"), table_name="finding_dispositions")
    op.drop_index(op.f("ix_finding_dispositions_source_id"), table_name="finding_dispositions")
    op.drop_index(op.f("ix_finding_dispositions_timeline_id"), table_name="finding_dispositions")
    op.drop_index(op.f("ix_finding_dispositions_case_id"), table_name="finding_dispositions")
    op.drop_table("finding_dispositions")
