"""Head/middle/tail excerpt with absolute line numbers, condensed to distinct line
shapes; refuses binary; sees through .gz."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from vestigo.converters.sample import (
    CONDENSE_KEEP_PER_SHAPE,
    NotTextError,
    assert_text_file,
    build_sample,
    safe_filename,
    sample_as_file,
)


def _raw(s):
    """The blocks as ``(label, first line number, text)`` — the pre-condensing shape."""
    return [(b.label, b.first, b.text) for b in s.blocks]


def _write(tmp_path: Path, n: int, name: str = "a.log") -> Path:
    p = tmp_path / name
    p.write_text("".join(f"line {i:05d} payload\n" for i in range(1, n + 1)))
    return p


def test_small_file_is_one_head_block(tmp_path):
    p = _write(tmp_path, 10)
    s = build_sample(p, budget_bytes=65536)
    assert [b.label for b in s.blocks] == ["head"]
    assert s.blocks[0].first == 1 and s.blocks[0].lines[-1] == (10, "line 00010 payload")
    assert s.line_count == 10 and s.size_bytes == p.stat().st_size
    assert len(s.sha256) == 64 and s.mtime_iso is None  # not told: never the staging copy's
    told = build_sample(p, budget_bytes=65536, mtime=1_700_000_000.0)
    assert told.mtime_iso == "2023-11-14T22:13:20Z"


def test_large_file_has_three_blocks_with_absolute_numbers(tmp_path):
    p = _write(tmp_path, 5000)
    s = build_sample(p, budget_bytes=4096, condense=False)
    labels = [b.label for b in s.blocks]
    assert labels == ["head", "middle", "tail"]
    head, middle, tail = s.blocks
    assert head.first == 1
    assert 1 < middle.first < tail.first <= 5000
    assert tail.text.splitlines()[-1] == "line 05000 payload"
    # Every line carries its own absolute number, and it matches the content.
    for block in s.blocks:
        for number, text in block.lines:
            assert text == f"line {number:05d} payload"
    assert len(s.text.encode()) <= 4096 + 3 * 200  # whole-line overhead only


def test_gzip_transparent(tmp_path):
    raw = "".join(f"l{i}\n" for i in range(50)).encode()
    p = tmp_path / "a.log.gz"
    p.write_bytes(gzip.compress(raw))
    s = build_sample(p, budget_bytes=65536)
    assert s.blocks[0].text.startswith("l0\n") and s.line_count == 50


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
    assert out.name == "app.log" and out.read_text() == s.text + "\n"


def test_single_huge_line_does_not_crash_and_stays_in_budget(tmp_path):
    # One 100 KB line, no newline at all (a minified JSON blob): line_count == 1
    # and no index may run past it; the excerpt must respect the budget too.
    p = tmp_path / "blob.json"
    p.write_bytes(b"x" * 100_000)
    s = build_sample(p, budget_bytes=65536)
    assert s.line_count == 1 and [b.label for b in s.blocks] == ["head"]
    assert len(s.text.encode()) <= 65536


def test_huge_last_line_cannot_blow_the_budget(tmp_path):
    p = tmp_path / "a.log"
    p.write_bytes(b"".join(b"line %05d\n" % i for i in range(5000)) + b"y" * 500_000)
    s = build_sample(p, budget_bytes=65536)
    assert len(s.text.encode()) <= 65536 + 3 * 200
    for block in s.blocks:
        assert 1 <= block.first <= 5001 and block.text


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
    s = build_sample(p, budget_bytes=budget, condense=False)
    assert _raw(s) == _reference_blocks(p, budget)
    assert s.line_count == len(data.splitlines()) if data else 0


_ACCESS = (
    '{ip} - - [05/Sep/2025:{h:02d}:{m:02d}:{s:02d} +0000] "GET {path} HTTP/1.1" {code} 229'
    ' "-" "Uptime-Kuma/1.23.16" "-"\n'
)


def _access_log(tmp_path: Path, n: int) -> Path:
    p = tmp_path / "access.log"
    p.write_text(
        "".join(
            _ACCESS.format(
                ip="87.123.40.45",
                h=i // 3600,
                m=(i // 60) % 60,
                s=i % 60,
                path="/auth/login" if i % 3 else "/",
                code=200 if i % 3 else 302,
            )
            for i in range(n)
        )
    )
    return p


def test_condensing_collapses_a_repetitive_log_to_its_shapes(tmp_path):
    # The bug this exists for: 3000 near-identical access-log lines were ~50k
    # tokens of prompt, all teaching the model the same one format.
    p = _access_log(tmp_path, 3000)
    raw = build_sample(p, budget_bytes=65536, condense=False)
    s = build_sample(p, budget_bytes=65536)
    assert len(s.text) * 8 < len(raw.text)
    kept = sum(len(b.lines) for b in s.blocks)
    assert kept <= CONDENSE_KEEP_PER_SHAPE * len(s.blocks) * 2
    # Nothing was rewritten or reordered: every kept line is the file's own.
    lines = p.read_text().splitlines()
    for block in s.blocks:
        numbers = [n for n, _t in block.lines]
        assert numbers == sorted(numbers)
        for number, text in block.lines:
            assert lines[number - 1] == text


def test_condensing_keeps_the_head_the_middle_and_the_tail(tmp_path):
    p = _access_log(tmp_path, 3000)
    s = build_sample(p, budget_bytes=4096)
    assert [b.label for b in s.blocks] == ["head", "middle", "tail"]
    assert s.blocks[0].first == 1
    assert s.blocks[-1].lines[-1][0] == 3000  # the tail still ends at EOF
    assert len(s.text.encode()) <= 4096


def test_condensing_keeps_every_distinct_shape(tmp_path):
    p = tmp_path / "mixed.log"
    p.write_text(
        "".join(f"2025-09-05T00:00:{i % 60:02d}Z INFO heartbeat ok\n" for i in range(300))
        + "2025-09-05T01:00:00Z ERROR disk full on /var\n"
        + "id,name,value\n"
    )
    s = build_sample(p, budget_bytes=4096)
    assert "ERROR disk full on /var" in s.text
    assert "id,name,value" in s.text
    # The cap is per shape *per block* — a shape that spans the file is shown
    # where each window falls, and nowhere more than the cap.
    for block in s.blocks:
        assert block.text.count("heartbeat ok") <= CONDENSE_KEEP_PER_SHAPE


def test_condensing_never_orphans_a_continuation_line(tmp_path):
    # A stack trace is one record: its indented lines must stay with the line
    # they belong to, however many times that shape has already been shown.
    p = tmp_path / "app.log"
    p.write_text(
        "".join(
            f"2025-09-05 00:00:{i:02d},000 ERROR boom\n    at com.example.Foo(Foo.java:{i})\n"
            for i in range(40)
        )
    )
    s = build_sample(p, budget_bytes=65536)
    for block in s.blocks:
        texts = [t for _n, t in block.lines]
        for i, text in enumerate(texts):
            if text.startswith("    at "):
                assert i > 0 and texts[i - 1].endswith("ERROR boom")


def test_sample_run_input_is_the_whole_condensed_excerpt(tmp_path):
    # Every block, so the guarded run sees the middle and the tail too.
    p = _access_log(tmp_path, 3000)
    s = build_sample(p, budget_bytes=65536)
    out = sample_as_file(s, tmp_path / "in", "access.log")
    written = out.read_text().splitlines()
    assert written == [t for b in s.blocks for _n, t in b.lines]
    assert len(s.blocks) == 3 and len(written) < 3000


def test_lines_that_break_parsers_survive_condensing(tmp_path):
    # An empty request and a TLS handshake sent to an HTTP port are the two
    # shapes a naive access-log regex dies on. Masking a quoted run to "S"
    # would have made them indistinguishable from every ordinary request.
    odd = [
        '103.20.103.50 - - [05/Sep/2025:14:58:27 +0000] "" 400 0 "-" "-" "-"',
        '93.174.93.12 - - [05/Sep/2025:00:51:16 +0000] "\\x16\\x03\\x02\\x01o" 400 157 "-" "-" "-"',
    ]
    p = tmp_path / "access.log"
    ordinary = _access_log(tmp_path, 2000).read_text().splitlines()
    p.write_text("\n".join(ordinary[:1000] + odd[:1] + ordinary[1000:] + odd[1:]) + "\n")
    s = build_sample(p, budget_bytes=65536)
    assert odd[0] in s.text and odd[1] in s.text


def test_condensing_marks_the_gap_it_left(tmp_path):
    from vestigo.converters.prompt import _render_sample

    p = _access_log(tmp_path, 3000)
    rendered = _render_sample(build_sample(p, budget_bytes=65536))
    numbers = [
        int(line.split("|", 1)[0])
        for line in rendered.splitlines()
        if "|" in line and line[:4].strip().isdigit()
    ]
    assert numbers == sorted(numbers)
    assert "elided" in rendered or numbers != list(range(numbers[0], numbers[-1] + 1))
