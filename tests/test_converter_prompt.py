"""The prompt is data → three renderings; nothing but the sample crosses the wire."""

from __future__ import annotations

from dataclasses import dataclass

from tests.conftest import as_admin
from vestigo.converters import prompt as P
from vestigo.ingestion.parquet_format import (
    META_CONVERTED_AT,
    META_CONVERTER_NAME,
    META_CONVERTER_VERSION,
    META_FORMAT_VERSION,
    META_ORIGINAL_FILES,
    META_PARSE_DECISIONS,
    META_ROW_COUNTS,
    META_TIMEZONE_ASSUMPTION,
)

ALL_KEYS = (
    META_FORMAT_VERSION,
    META_CONVERTER_NAME,
    META_CONVERTER_VERSION,
    META_ORIGINAL_FILES,
    META_CONVERTED_AT,
    META_ROW_COUNTS,
    META_TIMEZONE_ASSUMPTION,
    META_PARSE_DECISIONS,
)


@dataclass
class _S:
    blocks: list


SAMPLE = _S(
    blocks=[
        ("head", 1, "Jan  5 10:00:01 host sshd[1]: Accepted\nJan  5 10:00:02 host sshd[1]: Failed")
    ]
)
KW: dict = {
    "sample": SAMPLE,
    "filename": "auth.log",
    "size_bytes": 1234,
    "line_count": 2,
    "mtime_iso": "2026-01-05T10:00:00Z",
    "version": 2,
    "hint": "local time is Europe/Berlin",
}


def test_generation_prompt_carries_contract_and_sample_only():
    system, task = P.render_generation_prompt(**KW)
    for key in ALL_KEYS:
        assert key in system
    assert 'pa.field("byte_offset", pa.uint64())' in system
    assert "no network" in system.lower()
    assert "auth.log" in task and "1234" in task and "2.0.0" in task
    assert "Europe/Berlin" in task
    assert "Accepted" in task and "   1 | Jan  5" in task  # line-numbered
    for canary in ("case_", "user_id", "Authorization", "api_key"):
        assert canary not in task


def test_repair_prompt_carries_previous_script_and_report():
    system, task = P.render_repair_prompt(
        previous_script="print('v1')",
        report={
            "ok": False,
            "checks": [{"name": "footer", "ok": False, "detail": "missing vestigo.format_version"}],
        },
        stderr_tail="Traceback ...",
        **KW,
    )
    assert system == P.render_generation_prompt(**KW)[0]  # identical system message
    assert "print('v1')" in task and "missing vestigo.format_version" in task
    assert "Traceback" in task
    assert "complete replacement" in task.lower()


def test_human_prompts_keep_contract_elements():
    p = P.render_human_prompt_parquet()
    for key in ALL_KEYS:
        assert key in p
    assert '"path"' in p and '"mtime"' in p
    assert "document any input-timezone assumption at the top of the script" not in p
    assert "[PASTE A REPRESENTATIVE SAMPLE" in p
    assert "pipe-separated" in P.render_human_prompt_csv()


def test_prompt_endpoint(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    body = client.get("/api/converters/prompt").json()
    assert body["parquet"] == P.render_human_prompt_parquet()
    assert body["csv"] == P.render_human_prompt_csv()


def test_sample_header_states_the_real_block_ranges_not_a_literal():
    # The system message stays generic; the task names what the excerpt actually holds
    # (a head-only file has no "middle" or "end" to promise) and the shortening rule.
    three = _S(
        blocks=[
            ("head", 1, "a\nb\nc"),
            ("middle", 500, "d\ne"),
            ("tail", 998, "f\ng\nh"),
        ]
    )
    system, task = P.render_generation_prompt(**{**KW, "sample": three, "line_count": 1000})
    assert "few dozen" not in system and "the end of the file" not in system
    assert "SAMPLE (8 of 1000 lines: head 1-3, middle 500-501, tail 998-1000)" in task
    assert "more chars]" in task  # the shortening rule is stated where it applies
    _system, task = P.render_generation_prompt(**KW)
    assert "SAMPLE (2 of 2 lines: head 1-2)" in task
