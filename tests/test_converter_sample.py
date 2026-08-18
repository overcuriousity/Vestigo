"""Head/middle/tail excerpt with absolute line numbers; refuses binary; sees through .gz."""

from __future__ import annotations

import gzip
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
    assert len(s.sha256) == 64 and s.mtime_iso.endswith("Z")


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


def test_sample_as_file_writes_head_under_original_name(tmp_path):
    p = _write(tmp_path, 3000)
    s = build_sample(p, budget_bytes=2048)
    out = sample_as_file(s, tmp_path / "in", "app.log")
    assert out.name == "app.log" and out.read_text() == s.blocks[0][2] + "\n"


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
    assert gzip.decompress(out.read_bytes()) == s.blocks[0][2].encode() + b"\n"


def _reference_blocks(path: Path, budget_bytes: int) -> list[tuple[str, int, str]]:
    """The original per-line-offset algorithm, kept as the oracle for the streaming one."""
    import bisect

    from vestigo.converters.sample import _open

    offsets: list[int] = []
    pos = 0
    with _open(path) as fh:
        for line in fh:
            offsets.append(pos)
            pos += len(line)
    total, line_count = pos, len(offsets)

    def read_lines(start_idx: int, byte_budget: int) -> str:
        with _open(path) as fh:
            fh.seek(offsets[start_idx])
            chunk = fh.read(byte_budget)
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
    tail_idx = max(bisect.bisect_left(offsets, max(total - tail_b, 0)), next_idx)
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
    assert s.blocks == _reference_blocks(p, budget)
    assert s.line_count == len(data.splitlines()) if data else 0
