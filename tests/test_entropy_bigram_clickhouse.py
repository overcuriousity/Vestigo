"""The entropy detector's bigram variant catches what Shannon entropy cannot (D11).

A lowercase-latin DGA domain among English hostnames is built from perfectly
ordinary characters in an order English never produces: its Shannon entropy
sits inside the field's band, its mean bigram surprisal does not. Real
ClickHouse, since the claim is about the learned table and the SQL that
scores against it.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import insert_generated_events
from vestigo.db.anomaly_stats import StatisticalAnomalyService
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = pytest.mark.clickhouse

_CASE = f"bigram-{uuid.uuid4().hex[:8]}"
_SOURCE = "src-bigram"

#: English-looking hostnames: two words from a small vocabulary plus a
#: number, so the corpus has a few hundred distinct values and a stable
#: English bigram table.
_WORDS = (
    "mail",
    "print",
    "files",
    "backup",
    "web",
    "office",
    "reception",
    "finance",
    "sales",
    "support",
    "build",
    "test",
    "staging",
    "monitor",
    "gateway",
    "storage",
)
#: The DGA: lowercase latin, no vowels-in-the-right-places, English never
#: makes these pairs. Shannon entropy ≈ 3.5 bits — inside an English band.
_DGA = "kqzvxwmjplrtnb"


@pytest.fixture(scope="module")
def store():
    s = ClickHouseStore()
    s.init_schema()
    words = ", ".join(f"'{w}'" for w in _WORDS)
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_SOURCE,
        n=4000,
        attributes=(
            "map('host', if(number = 777, "
            f"'{_DGA}.corp.local', "
            f"concat(arrayElement([{words}], toUInt32(cityHash64(number) % {len(_WORDS)}) + 1), "
            f"'-', arrayElement([{words}], toUInt32(cityHash64(number * 7) % {len(_WORDS)}) + 1), "
            "toString(number % 20), '.corp.local')))"
        ),
    )
    yield s
    s.delete_source_events(_CASE, _SOURCE)


def test_bigram_variant_flags_the_dga_that_shannon_misses(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    shannon = svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:host"], limit=50)
    assert shannon.status == "ok"
    assert shannon.method == "iqr"
    assert not any(f.value.startswith(_DGA) for f in shannon.results), [
        f.value for f in shannon.results
    ]

    bigram = svc.find_entropy_outliers(
        _CASE, [_SOURCE], fields=["attr:host"], limit=50, variant="bigram"
    )
    assert bigram.status == "ok", bigram.warnings
    assert bigram.method == "bigram-iqr"
    assert bigram.results, bigram.warnings
    top = bigram.results[0]
    assert top.value.startswith(_DGA)
    assert top.direction == "above"
    assert top.details["variant"] == "bigram"
    assert top.details["bigram_table"] == top.details["bigram_distinct"]
    assert not any("capped" in w for w in bigram.warnings)


def test_bigram_variant_is_reproducible(store):
    svc = StatisticalAnomalyService(clickhouse=store)
    a = svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:host"], variant="bigram")
    b = svc.find_entropy_outliers(_CASE, [_SOURCE], fields=["attr:host"], variant="bigram")
    assert [f.details for f in a.results] == [f.details for f in b.results]
