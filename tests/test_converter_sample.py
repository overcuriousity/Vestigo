"""Head/middle/tail excerpt with absolute line numbers; refuses binary; sees through .gz."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from vestigo.converters.sample import NotTextError, assert_text_file, build_sample, sample_as_file


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
