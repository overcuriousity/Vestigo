"""Head/middle/tail excerpt with absolute line numbers; refuses binary; sees through .gz."""

from __future__ import annotations

import gzip
import json
import textwrap
from pathlib import Path

import pytest

from vestigo.converters.sample import (
    NotTextError,
    assert_text_file,
    build_sample,
    safe_filename,
    sample_as_file,
)


def _write(tmp_path: Path, n: int, name: str = "a.log") -> Path:
    p = tmp_path / name
    p.write_text("".join(f"line {i:05d} payload\n" for i in range(1, n + 1)))
    return p


def test_small_file_is_one_head_block(tmp_path):
    p = _write(tmp_path, 10)
    s = build_sample(p, budget_bytes=65536)
    assert [b[0] for b in s.blocks] == ["head"]
    assert s.blocks[0][1] == 1 and s.blocks[0][2].splitlines()[-1] == "line 00010 payload"
    assert s.line_count == 10 and s.size_bytes == p.stat().st_size
    assert len(s.sha256) == 64 and s.mtime_iso is None  # not told: never the staging copy's
    told = build_sample(p, budget_bytes=65536, mtime=1_700_000_000.0)
    assert told.mtime_iso == "2023-11-14T22:13:20Z"


def test_large_file_has_three_blocks_with_absolute_numbers(tmp_path):
    p = _write(tmp_path, 5000)
    s = build_sample(p, budget_bytes=4096)
    labels = [b[0] for b in s.blocks]
    assert labels == ["head", "middle", "tail"]
    head, middle, tail = s.blocks
    assert head[1] == 1
    assert 1 < middle[1] < tail[1] <= 5000
    assert tail[2].splitlines()[-1] == "line 05000 payload"
    # Each block's first line number matches its content.
    for _label, first, text in s.blocks:
        assert text.splitlines()[0] == f"line {first:05d} payload"
    assert len(s.text.encode()) <= 4096 + 3 * 200  # whole-line overhead only


def test_gzip_transparent(tmp_path):
    raw = "".join(f"l{i}\n" for i in range(50)).encode()
    p = tmp_path / "a.log.gz"
    p.write_bytes(gzip.compress(raw))
    s = build_sample(p, budget_bytes=65536)
    assert s.blocks[0][2].startswith("l0\n") and s.line_count == 50


def test_binary_refused(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(NotTextError):
        build_sample(p, budget_bytes=4096)
    with pytest.raises(NotTextError):
        assert_text_file(p)


def test_empty_refused(tmp_path):
    p = tmp_path / "e.log"
    p.write_bytes(b"")
    with pytest.raises(NotTextError):
        assert_text_file(p)


def test_sample_as_file_writes_every_block_under_original_name(tmp_path):
    # Head, middle and tail: the guarded sample run sees the newest lines too.
    p = _write(tmp_path, 3000)
    s = build_sample(p, budget_bytes=2048)
    out = sample_as_file(s, tmp_path / "in", "app.log")
    assert out.name == "app.log" and out.read_text() == s.text + "\n"
    assert [b[0] for b in s.blocks] == ["head", "middle", "tail"]
    assert out.read_text().splitlines()[-1] == "line 03000 payload"


def test_default_budget_is_a_few_dozen_lines(tmp_path):
    # The default exists to keep the prompt small: ~3 KiB of a 95-byte-per-line
    # access log is a few dozen lines, not the thousands that timed a local model out.
    from vestigo.core.config import Settings

    budget = Settings.model_fields["converter_sample_bytes"].default
    line = '10.0.0.1 - - [05/Sep/2025:10:00:00 +0000] "GET /auth/login HTTP/1.1" 200 229 "-" "UA"\n'
    p = tmp_path / "access.log"
    p.write_text(line * 3000)
    s = build_sample(p, budget_bytes=budget)
    assert 20 <= s.text.count("\n") + 1 <= 60
    assert len(s.text.encode()) <= budget


def test_budget_floor_is_the_default():
    # Below 4 KiB the 70/15/15 split cannot hold a whole ordinary line per block.
    from pydantic import ValidationError

    from vestigo.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(converter_sample_bytes=1024)


# ── Whole records: the budget bounds what is *sent*, never what a block may hold ──


def _jsonl_record(i: int, pad: int) -> str:
    return json.dumps({"ts": f"2026-03-01T10:00:{i % 60:02d}Z", "seq": i, "body": "x" * pad})


def test_long_jsonl_lines_are_whole_records_at_default_budget(tmp_path):
    # 1.5 KB per record (a session log, a CEF audit line): every block still holds
    # at least one whole record, and the sample-run file is valid line for line.
    p = tmp_path / "events.jsonl"
    p.write_text("".join(_jsonl_record(i, 1400) + "\n" for i in range(50)))
    s = build_sample(p, budget_bytes=4096)
    assert [b[0] for b in s.blocks] == ["head", "middle", "tail"]
    assert [b[0] for b in s.raw_blocks] == ["head", "middle", "tail"]
    for _label, _first, raw in s.raw_blocks:
        for line in raw.split("\n"):
            assert json.loads(line)["seq"] >= 0
    out = sample_as_file(s, tmp_path / "in", "events.jsonl")
    lines = out.read_text().splitlines()
    assert lines and all(json.loads(line)["seq"] >= 0 for line in lines)
    assert json.loads(lines[-1])["seq"] == 49  # the tail is the newest record
    assert len(s.text.encode()) <= 4096 + 3 * 64


def test_record_longer_than_the_whole_budget_is_still_whole(tmp_path):
    # A 3 KB record at 4 KiB: the head is one whole record (not a 2867-byte cut).
    p = tmp_path / "wide.jsonl"
    p.write_text("".join(_jsonl_record(i, 3000) + "\n" for i in range(4)))
    s = build_sample(p, budget_bytes=4096)
    head = s.raw_blocks[0][2]
    assert json.loads(head.split("\n")[0])["seq"] == 0
    out = sample_as_file(s, tmp_path / "in", "wide.jsonl")
    assert all(json.loads(line) for line in out.read_text().splitlines())


def test_last_line_longer_than_the_tail_budget_still_gives_a_tail(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("".join(f"line {i:05d}\n" for i in range(1, 501)) + "z" * 3000 + "\n")
    s = build_sample(p, budget_bytes=4096)
    assert s.blocks[-1][0] == "tail"
    assert s.raw_blocks[-1][2] == "z" * 3000 and s.raw_blocks[-1][1] == 501


def test_shown_lines_are_cut_at_the_block_budget(tmp_path):
    # What leaves the host stays inside the budget: a whole raw record, a cut shown one.
    p = tmp_path / "wide.log"
    p.write_text("".join(f"{i:04d} " + "y" * 3000 + "\n" for i in range(40)))
    s = build_sample(p, budget_bytes=4096)
    for (_l, _f, shown), (_l2, _f2, raw) in zip(s.blocks, s.raw_blocks, strict=True):
        assert raw.startswith(shown[:4]) and len(raw) >= 3000
        assert "…[" in shown and "more chars]" in shown
    assert len(s.text.encode()) <= 4096 + 3 * 64


def test_json_records_are_shown_with_long_values_shortened(tmp_path):
    rec = {"ts": "2026-03-01T10:00:00Z", "msg": "m" * 5000, "items": list(range(20)), "n": 1}
    p = tmp_path / "e.jsonl"
    p.write_text((json.dumps(rec) + "\n") * 30)
    s = build_sample(p, budget_bytes=4096)
    shown = s.blocks[0][2].split("\n")[0]
    assert "…[" in shown and "more chars]" in shown and "more items" in shown
    for key in rec:
        assert f'"{key}"' in shown
    assert "2026-03-01T10:00:00Z" in shown
    assert json.loads(s.raw_blocks[0][2].split("\n")[0]) == rec


def _pretty(i: int) -> str:
    return json.dumps(
        {"ts": f"2026-03-01T10:{i // 60:02d}:{i % 60:02d}Z", "seq": i, "d": {"a": [1, 2]}}, indent=2
    )


def _objects(text: str) -> list[dict]:
    dec = json.JSONDecoder()
    out, pos = [], 0
    while pos < len(text):
        while text[pos] in " \n,":
            pos += 1
        obj, end = dec.raw_decode(text, pos)
        out.append(obj)
        pos = end
        while pos < len(text) and text[pos] in " \n,":
            pos += 1
    return out


def test_pretty_printed_json_objects_split_on_records(tmp_path):
    p = tmp_path / "events.json"
    p.write_text("".join(_pretty(i) + "\n" for i in range(400)))
    s = build_sample(p, budget_bytes=4096)
    assert [b[0] for b in s.blocks] == ["head", "middle", "tail"]
    all_lines = p.read_text().split("\n")
    for _label, first, raw in s.raw_blocks:
        objs = _objects(raw)
        assert objs and all("seq" in o for o in objs)
        assert all_lines[first - 1] == "{" and raw.startswith(
            "{"
        )  # record boundary + absolute line
    assert _objects(s.raw_blocks[-1][2])[-1]["seq"] == 399
    out = sample_as_file(s, tmp_path / "in", "events.json")
    assert len(_objects(out.read_text())) == sum(len(_objects(b[2])) for b in s.raw_blocks)


def test_pretty_printed_top_level_array(tmp_path):
    p = tmp_path / "export.json"
    p.write_text(
        "[\n" + ",\n".join(textwrap.indent(_pretty(i), "  ") for i in range(400)) + "\n]\n"
    )
    s = build_sample(p, budget_bytes=4096)
    assert [b[0] for b in s.blocks] == ["head", "middle", "tail"]
    for _label, _first, raw in s.raw_blocks:
        assert all("seq" in o for o in _objects(raw))
    out = sample_as_file(s, tmp_path / "in", "export.json")
    items = json.loads(out.read_text())  # a valid array, like the file itself
    assert isinstance(items, list) and items[0]["seq"] == 0 and items[-1]["seq"] == 399
    assert len(items) == sum(len(_objects(b[2])) for b in s.raw_blocks)


def test_one_line_array_is_a_head_of_whole_elements(tmp_path):
    p = tmp_path / "export.json"
    p.write_text(json.dumps([{"seq": i, "body": "b" * 200} for i in range(500)]))
    s = build_sample(p, budget_bytes=4096)
    assert [b[0] for b in s.blocks] == ["head"]
    assert len(s.text.encode()) <= 4096 + 64
    out = sample_as_file(s, tmp_path / "in", "export.json")
    items = json.loads(out.read_text())
    assert isinstance(items, list) and 1 <= len(items) < 500 and items[0]["seq"] == 0


def test_quoted_csv_seam_blocks_are_dropped(tmp_path):
    # A block starting inside a quoted multi-line field is not a record: drop it
    # rather than hand the sample run a shape the file never has.
    rows = "".join(f'{i},"first line\nsecond line\nthird line",{i * 2}\n' for i in range(2000))
    p = tmp_path / "multi.csv"
    p.write_text("id,text,double\n" + rows)
    s = build_sample(p, budget_bytes=4096)
    for _label, _first, raw in s.raw_blocks[1:]:
        assert raw.count('"') % 2 == 0
        assert raw.startswith(("first line", "second line", "third line")) is False


def test_single_huge_line_does_not_crash_and_stays_in_budget(tmp_path):
    # One 100 KB line, no newline at all (a minified JSON blob): line_count == 1
    # and no index may run past it; the excerpt must respect the budget too.
    p = tmp_path / "blob.json"
    p.write_bytes(b"x" * 100_000)
    s = build_sample(p, budget_bytes=65536)
    assert s.line_count == 1 and [b[0] for b in s.blocks] == ["head"]
    assert len(s.text.encode()) <= 65536


def test_huge_last_line_cannot_blow_the_budget(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"".join(b"line %05d\n" % i for i in range(5000)) + b"y" * 500_000)
    s = build_sample(p, budget_bytes=65536)
    assert len(s.text.encode()) <= 65536 + 3 * 200
    for _label, first, text in s.blocks:
        assert 1 <= first <= 5001 and text


def test_safe_filename_strips_directories():
    assert safe_filename("../../../../home/app/.bashrc") == ".bashrc"
    assert safe_filename("/etc/passwd") == "passwd"
    assert safe_filename("C:\\evil\\x.log") == "x.log"
    assert safe_filename("..") == "input.log"
    assert safe_filename("") == "input.log"
    assert safe_filename(None) == "input.log"
    assert safe_filename("auth.log.gz") == "auth.log.gz"


def test_sample_as_file_never_leaves_dest_dir(tmp_path):
    p = _write(tmp_path, 3)
    s = build_sample(p, budget_bytes=2048)
    out = sample_as_file(s, tmp_path / "in", "../../escape.log")
    assert out.parent == tmp_path / "in" and out.name == "escape.log"


def test_sample_as_file_keeps_gz_encoding_for_gz_uploads(tmp_path):
    raw = "".join(f"l{i}\n" for i in range(50)).encode()
    p = tmp_path / "a.log.gz"
    p.write_bytes(gzip.compress(raw))
    s = build_sample(p, budget_bytes=65536)
    out = sample_as_file(s, tmp_path / "in", "a.log.gz")
    # Same name and same encoding as the full file: a script that handles .gz
    # by suffix behaves identically in the sample and the full run.
    assert out.name == "a.log.gz"
    assert gzip.decompress(out.read_bytes()) == s.text.encode() + b"\n"


def _reference_blocks(path: Path, budget_bytes: int) -> list[tuple[str, int, str]]:
    """A per-line-offset oracle for the streaming scan: whole lines, first line always whole."""
    import bisect

    from vestigo.converters.sample import _open

    offsets: list[int] = []
    pos = 0
    with _open(path) as fh:
        for line in fh:
            offsets.append(pos)
            pos += len(line)
    total, line_count = pos, len(offsets)
    ends = offsets[1:] + [total]

    def read_lines(start_idx: int, byte_budget: int) -> str:
        # At least the first line, however long; then whole lines while they fit.
        end = max(ends[start_idx], offsets[start_idx] + byte_budget)
        with _open(path) as fh:
            fh.seek(offsets[start_idx])
            chunk = fh.read(end - offsets[start_idx])
        text = chunk.decode("utf-8", errors="replace")
        if not text.endswith("\n") and offsets[start_idx] + len(chunk) < total and "\n" in text:
            text = text[: text.rfind("\n") + 1]
        return text.rstrip("\n")

    if total <= budget_bytes:
        return [("head", 1, read_lines(0, total) if line_count else "")]
    head_b = int(budget_bytes * 0.70)
    mid_b = int(budget_bytes * 0.15)
    tail_b = budget_bytes - head_b - mid_b
    head_text = read_lines(0, head_b)
    head_lines = head_text.count("\n") + 1
    blocks = [("head", 1, head_text)]
    next_idx = head_lines
    mid_idx = max(max(bisect.bisect_right(offsets, total // 2) - 1, 0), head_lines)
    if mid_idx < line_count:
        mid_text = read_lines(mid_idx, mid_b)
        blocks.append(("middle", mid_idx + 1, mid_text))
        next_idx = mid_idx + mid_text.count("\n") + 1
    tail_idx = bisect.bisect_left(offsets, max(total - tail_b, 0))
    if tail_idx >= line_count:
        tail_idx = line_count - 1  # nothing starts in the last bytes: the last line
    tail_idx = max(tail_idx, next_idx)
    if tail_idx < line_count:
        tail_text = read_lines(tail_idx, min(total - offsets[tail_idx], tail_b))
        blocks.append(("tail", tail_idx + 1, tail_text))
    return blocks


@pytest.mark.parametrize(
    "content,gz",
    [
        ("".join(f"line {i:05d} payload\n" for i in range(1, 5001)), False),
        ("".join(f"line {i:05d} payload\n" for i in range(1, 5001)), True),
        ("".join(f"l{i}\n" for i in range(3000)) + "no trailing newline", False),
        ("".join(f"line {i}\r\n" for i in range(3000)), False),
        ("x" * 20000 + "\n" + "".join(f"m{i}\n" for i in range(500)) + "y" * 9000, False),
        ("a" * 50000, False),
        ("".join(f"é{i} ünïcode\n" for i in range(4000)), False),
        ("".join(f"l{i}\n" for i in range(40)), False),
    ],
)
@pytest.mark.parametrize("budget", [1024, 4096, 65536])
def test_streaming_scan_matches_reference(tmp_path, content, gz, budget):
    p = tmp_path / ("a.log.gz" if gz else "a.log")
    data = content.encode()
    p.write_bytes(gzip.compress(data) if gz else data)
    s = build_sample(p, budget_bytes=budget)
    assert s.raw_blocks == _reference_blocks(p, budget)
    assert s.line_count == len(data.splitlines()) if data else 0


def test_shown_share_is_filled_with_whole_records_when_raw_is_bulky(tmp_path):
    # 3 KB records that show as ~300 bytes: the head's 2.8 KB share holds several
    # shown records, not just the one whose raw text exhausted the byte read.
    p = tmp_path / "bulky.jsonl"
    p.write_text("".join(_jsonl_record(i, 3000) + "\n" for i in range(60)))
    s = build_sample(p, budget_bytes=4096)
    head_raw = s.raw_blocks[0][2].split("\n")
    assert len(head_raw) >= 4 and all(
        json.loads(line)["seq"] == i for i, line in enumerate(head_raw)
    )
    assert s.blocks[0][2].count("\n") + 1 == len(head_raw)
    assert s.record_lines[0] == list(range(1, len(head_raw) + 1))
    assert len(s.blocks[0][2].encode()) <= int(4096 * 0.70)
