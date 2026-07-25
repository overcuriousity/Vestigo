"""Shared test doubles for the transfer (X1) test suite.

`_add` fills unknown non-nullable columns so tests survive model churn;
`FakeClickHouse` stands in for ClickHouseStore keyed by (case_id, source_id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def _fill(col):
    """Value for a non-nullable column without a default, by Python type."""
    t = col.type.python_type
    if t is str:
        return "x"
    if t is int:
        return 0
    if t is bool:
        return False
    if t is dict:
        return {}
    if t is list:
        return []
    if t is datetime:
        return datetime.now(UTC)
    raise AssertionError(f"unhandled column type {col}")


async def _add(store, model, **overrides):
    values = {}
    for col in model.__table__.columns:
        if col.name in overrides:
            values[col.name] = overrides.pop(col.name)
        elif col.primary_key:
            values[col.name] = f"{model.__tablename__}-{uuid.uuid4().hex[:8]}"
        elif col.nullable or col.default is not None or col.server_default is not None:
            continue
        else:
            values[col.name] = _fill(col)
    assert not overrides, f"unknown columns for {model.__tablename__}: {overrides}"
    async with store.session_factory() as session:
        obj = model(**values)
        session.add(obj)
        await session.commit()
        await session.refresh(obj)
        return obj


class FakeClickHouse:
    """Stands in for ClickHouseStore; keyed by (case_id, source_id)."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.inserted = {}
        self.deleted = []

    def iter_source_events(self, case_id, source_id, batch_size):
        rows = self.rows.get((case_id, source_id), [])
        for i in range(0, len(rows), batch_size):
            yield rows[i : i + batch_size]

    def insert_events_arrow(self, batch):
        for r in batch.to_pylist():
            self.inserted.setdefault((r["case_id"], r["source_id"]), []).append(r)
        return batch.num_rows

    def delete_source_events(self, case_id, source_id):
        self.deleted.append((case_id, source_id))


def _event_rows(case_id, source_id, n=2):
    return [
        {
            "event_id": uuid.uuid4(),
            "case_id": case_id,
            "source_id": source_id,
            "source_file": "demo.log",
            "byte_offset": i * 100,
            "line_number": i,
            "content_hash": (f"{i:064d}").encode(),
            "file_hash": b"ab" * 32,
            "parser_name": "demo",
            "parser_version": "1",
            "ingest_time": "2026-07-24T10:00:00+00:00",
            "message": f"line {i}",
            "timestamp": f"2026-07-24T09:00:0{i}+00:00",
            "timestamp_desc": "parsed",
            "artifact": "x",
            "artifact_long": "y",
            "display_name": "d",
            "tags": ["t1"],
            "attributes": {"k": "v"},
            "embedding_model": "",
            "embedding_config_hash": "",
        }
        for i in range(n)
    ]
