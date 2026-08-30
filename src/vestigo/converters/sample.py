"""Bounded excerpt of a raw text log for the model, plus text/binary detection.

Head takes 70% of the budget, a middle window 15%, the tail 15% — formats change
mid-file and the tail shows the newest timestamps. Absolute line numbers, so the
model can cite them. ``.gz`` is read transparently.

Two things are bounded, and they are not the same thing:

* ``Sample.raw_blocks`` hold **whole records** — the guarded sample run gets
  exactly these (:func:`sample_as_file`), so a script never sees a cut line or
  a block that starts inside a record. A block holds at least one record even
  when that record alone is longer than the block's share of the budget.
* ``Sample.blocks`` are what the model sees, and ``converter_sample_bytes``
  bounds *them*: a record longer than its block's share is cut with an
  ``…[N more chars]`` marker, and a JSON record is shown with long string
  values and long arrays shortened the same way — every key, every level of
  nesting, none of the bulk. ``docs/INPUT_FORMATS.md`` §"The loop" step 1
  says why the budget is small.

A *record* starts after a marker: ``\\n`` for line-oriented text (syslog, CLF,
JSON per line), ``\\n<indent>{`` for pretty-printed JSON objects or the elements
of a pretty-printed top-level array. A one-line ``[{…},{…}]`` array has no
markers; its head is the first elements, decoded, and nothing else. All
positions come from one streaming pass over the file (O(1) memory).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

_PROBE_BYTES = 8192

#: Shown-string prefix and the length above which a string is shortened.
_STR_KEEP = 100
_STR_MAX = 160
#: Shown array items and the length above which an array is shortened.
_LIST_KEEP = 3
_LIST_MAX = 8
#: A record longer than this is cut even in the raw sample file.
_MAX_RECORD_BYTES = 1 << 20

LAYOUT_LINES = "lines"
LAYOUT_JSONL = "jsonl"
LAYOUT_JSON_OBJECTS = "json_objects"
LAYOUT_JSON_ARRAY = "json_array"
LAYOUT_JSON_ARRAY_INLINE = "json_array_inline"


class NotTextError(ValueError):
    """The file does not look like text (empty, NUL bytes, or mostly non-printable)."""


@dataclass(frozen=True)
class Sample:
    """The excerpt sent to the model and the facts the task header states."""

    #: What the model sees: ``(label, first absolute line, shown text)``.
    blocks: list[tuple[str, int, str]]
    #: The same blocks as whole raw records — the sample-run input.
    raw_blocks: list[tuple[str, int, str]]
    #: Per block, the absolute line number of each shown line.
    record_lines: list[list[int]]
    text: str
    size_bytes: int
    line_count: int
    #: The evidence file's own mtime as ISO-8601 UTC, or ``None`` when the
    #: caller could not learn it (a raw API upload carries none) — never the
    #: staging copy's, which is just the upload time.
    mtime_iso: str | None
    sha256: str
    layout: str = LAYOUT_LINES


@dataclass(frozen=True)
class _Layout:
    kind: str
    marker: bytes
    #: Lines layout only: the probe showed a line with an odd number of ``"``
    #: (a quoted multi-line CSV field), so a record boundary is a newline with
    #: an even count of ``"`` before it in the file — never a line inside a field.
    quoted: bool = False
    #: Element indent (array) — the bytes between the newline and the ``{``.
    indent: bytes = b""
    #: Indent inside a record, for showing a pretty-printed record the same way.
    inner_indent: str | None = None


def _open(path: Path) -> BinaryIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


def _assert_text(head: bytes) -> None:
    if not head:
        raise NotTextError("file is empty")
    if b"\x00" in head:
        raise NotTextError("file contains NUL bytes")
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    if printable / len(head) < 0.7:
        raise NotTextError("file is mostly non-printable")


def assert_text_file(path: Path) -> None:
    """Cheap request-time check: only the first 8 KiB are read."""
    with _open(path) as fh:
        _assert_text(fh.read(_PROBE_BYTES))


def _is_json(text: bytes) -> bool:
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def _leading_ws(line: bytes) -> bytes:
    return line[: len(line) - len(line.lstrip(b" \t"))]


def _detect_layout(probe: bytes, total: int) -> _Layout:
    """Decide how records are delimited from the first 8 KiB alone.

    ``{`` first: one object per line when the first line is complete JSON,
    else pretty-printed objects starting at column 0. ``[`` first: one
    element per ``\\n<indent>{`` when the array spans lines, else one inline
    array. Anything else is lines.
    """
    stripped = probe.lstrip(b" \t\r\n")
    first_line, _, _rest = stripped.partition(b"\n")
    if stripped[:1] == b"{":
        if _is_json(first_line):
            return _Layout(LAYOUT_JSONL, b"\n")
        lines = stripped.split(b"\n")
        inner = _leading_ws(lines[1]) if len(lines) > 1 else b""
        return _Layout(LAYOUT_JSON_OBJECTS, b"\n{", inner_indent=inner.decode() or None)
    if stripped[:1] == b"[":
        if len(first_line) >= total - (len(probe) - len(stripped)) or _is_json(first_line):
            return _Layout(LAYOUT_JSON_ARRAY_INLINE, b"")
        if first_line.strip() == b"[":
            lines = stripped.split(b"\n")
            for i, line in enumerate(lines[1:], start=1):
                if line.lstrip(b" \t").startswith(b"{"):
                    indent = _leading_ws(line)
                    inner = _leading_ws(lines[i + 1]) if len(lines) > i + 1 else b""
                    return _Layout(
                        LAYOUT_JSON_ARRAY,
                        b"\n" + indent + b"{",
                        indent=indent,
                        inner_indent=inner.decode() or None,
                    )
        return _Layout(LAYOUT_JSON_ARRAY_INLINE, b"")
    lines = probe.split(b"\n")[:-1]
    return _Layout(LAYOUT_LINES, b"\n", quoted=any(line.count(b'"') % 2 for line in lines))


_SCAN_CHUNK = 1 << 20
#: How many candidate newlines a quote-parity search walks before settling for the nearest.
_QUOTE_STEPS = 64


class _Threshold:
    """What the excerpt needs to know about one byte position ``at`` in the stream.

    Fed the stream chunk by chunk (never overlapping). ``last_before`` is the
    start of the last marker in ``data[:at]`` (-1 if none) and ``first_from``
    the start of the first marker at or after ``at`` (``None`` if none); the
    ``*_line`` values are the absolute line number of the record that starts
    right after that marker.
    """

    def __init__(self, at: int, marker: bytes, quoted: bool = False) -> None:
        self.at = at
        self.marker = marker
        self.quoted = quoted
        self.last_before = -1
        self.last_before_line = 1
        self.first_from: int | None = None
        self.first_from_line = 1

    def _even(self, chunk: bytes, nl: int, quotes_before: int) -> bool:
        return not self.quoted or (quotes_before + chunk[: nl + 1].count(b'"')) % 2 == 0

    def feed(self, chunk: bytes, base: int, newlines_before: int, quotes_before: int) -> None:
        end = base + len(chunk)
        if base < self.at:
            head = chunk[: self.at - base]
            limit = len(head)
            nearest = -1
            for _ in range(_QUOTE_STEPS):
                nl = head.rfind(self.marker, 0, limit)
                if nl < 0:
                    break
                nearest = nl if nearest < 0 else nearest
                if self._even(head, nl, quotes_before):
                    nearest = nl
                    break
                limit = nl
            if nearest >= 0:
                self.last_before = base + nearest
                self.last_before_line = newlines_before + head[: nearest + 1].count(b"\n") + 1
        if end > self.at and self.first_from is None:
            start = max(self.at - base, 0)
            nearest = -1
            for _ in range(_QUOTE_STEPS):
                nl = chunk.find(self.marker, start)
                if nl < 0:
                    break
                nearest = nl if nearest < 0 else nearest
                if self._even(chunk, nl, quotes_before):
                    nearest = nl
                    break
                start = nl + 1
            if nearest >= 0:
                self.first_from = base + nearest
                self.first_from_line = newlines_before + chunk[: nearest + 1].count(b"\n") + 1


@dataclass
class _Scan:
    """What one streaming pass learns about the file (O(1) memory)."""

    total: int
    line_count: int
    #: ``middle``: the record containing byte M — its first line and start offset.
    mid_line: int
    mid_off: int
    #: ``tail``: the first record starting at or after byte T (``None`` offset
    #: when no record starts there) …
    tail_line: int
    tail_off: int | None
    #: … and the record containing the file's last byte, the fallback.
    last_line: int
    last_off: int


def _scan(
    path: Path,
    marker: bytes = b"\n",
    mid_at: int | None = None,
    tail_at: int | None = None,
    last_at: int | None = None,
    quoted: bool = False,
) -> _Scan:
    """Count lines in fixed-size chunks and resolve the byte thresholds.

    Replaces a per-line offset list — 100M lines meant ~4 GB of ints in the API
    process for a file at the upload cap. Everything the excerpt needs from the
    whole file is its length, its line count and three (line, offset) pairs, so
    that is all this keeps. The thresholds may be omitted when the length is
    not known up front (a ``.gz``): the caller scans once for the length and
    once more with the thresholds. A multi-byte marker never straddles two
    chunks: the last ``len(marker) - 1`` bytes of a chunk are carried into the next.
    """
    total = 0
    newlines = 0
    quotes = 0
    last_byte = b""
    mid = _Threshold(mid_at, marker, quoted) if mid_at is not None else None
    # A record starts at or after T iff its marker starts at or after T-1.
    tail = _Threshold(max(tail_at - 1, 0), marker, quoted) if tail_at is not None else None
    last = _Threshold(last_at, marker, quoted) if last_at is not None else None
    keep = max(len(marker) - 1, 0)
    carry = b""
    with _open(path) as fh:
        while True:
            chunk = fh.read(_SCAN_CHUNK)
            buf = carry + chunk
            if not chunk:
                carry = b""
            else:
                carry = buf[len(buf) - keep :] if keep else b""
                buf = buf[: len(buf) - len(carry)]
            for t in (mid, tail, last):
                if t is not None:
                    t.feed(buf, total, newlines, quotes)
            newlines += buf.count(b"\n")
            if quoted:
                quotes += buf.count(b'"')
            total += len(buf)
            if buf:
                last_byte = buf[-1:]
            if not chunk:
                break
    line_count = newlines + (1 if last_byte and last_byte != b"\n" else 0)
    mid_line, mid_off = (mid.last_before_line, mid.last_before + 1) if mid else (1, 0)
    tail_line, tail_off = 1, 0
    if tail is not None:
        if tail_at == 0:
            tail_line, tail_off = 1, 0
        elif tail.first_from is not None and tail.first_from + 1 < total:
            tail_line, tail_off = tail.first_from_line, tail.first_from + 1
        else:
            tail_off = None  # nothing starts there (or only the file's final newline)
    last_line, last_off = (last.last_before_line, last.last_before + 1) if last else (1, 0)
    return _Scan(total, line_count, mid_line, mid_off, tail_line, tail_off, last_line, last_off)


def count_lines(path: Path) -> int:
    """Line count of a (possibly gzipped) text file, streamed."""
    return _scan(path).line_count


def mtime_to_iso(mtime: float | None) -> str | None:
    """``None`` stays ``None``; a POSIX timestamp becomes ``YYYY-MM-DDTHH:MM:SSZ``."""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Showing a record ──────────────────────────────────────────────────────────


def _cut(text: str, keep: int) -> str:
    if len(text) <= keep:
        return text
    return f"{text[:keep]}…[{len(text) - keep} more chars]"


def _shorten(value: object) -> object:
    """Same keys, same nesting, same types; long strings and arrays cut with a marker."""
    if isinstance(value, str):
        return _cut(value, _STR_KEEP) if len(value) > _STR_MAX else value
    if isinstance(value, dict):
        return {k: _shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _LIST_MAX:
            return [_shorten(v) for v in value[:_LIST_KEEP]] + [
                f"…[{len(value) - _LIST_KEEP} more items]"
            ]
        return [_shorten(v) for v in value]
    return value


def _show_record(raw: str, layout: _Layout, cap: int) -> str:
    """One record as the model sees it: shortened JSON when it parses, else cut text."""
    if layout.kind != LAYOUT_LINES:
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = None
        if obj is not None:
            indent = layout.inner_indent if layout.kind in _PRETTY else None
            shown = json.dumps(_shorten(obj), ensure_ascii=False, indent=indent)
            return _cut(shown, cap)
    return _cut(raw, cap)


_PRETTY = {LAYOUT_JSON_OBJECTS, LAYOUT_JSON_ARRAY}


def _strip_record(raw: str, layout: _Layout) -> str:
    """An array element's raw text without its separator or the closing ``]``."""
    if layout.kind != LAYOUT_JSON_ARRAY:
        return raw
    s = raw.rstrip()
    if s.endswith("]"):
        s = s[:-1].rstrip()
    return s.removesuffix(",")


# ── Building the excerpt ──────────────────────────────────────────────────────


def build_sample(path: Path, budget_bytes: int, *, mtime: float | None = None) -> Sample:
    """Return the excerpt sent to the model; raises :class:`NotTextError` for binary.

    ``mtime`` is the evidence file's modification time as the uploader knew it
    (``File.lastModified`` in the browser, ``stat`` on the CLI). It is *not*
    read from ``path``: that is a staging copy whose mtime is the upload time,
    and the prompt tells the model to take a missing year from the mtime.
    """
    with _open(path) as fh:
        probe = fh.read(_PROBE_BYTES)
    _assert_text(probe)
    # The thresholds depend on the decompressed length, which only a pass over
    # a ``.gz`` reveals; a plain file's length is its size on disk.
    first = _scan(path) if path.suffix == ".gz" else None
    total = first.total if first else path.stat().st_size
    layout = _detect_layout(probe, total)
    mtime_iso = mtime_to_iso(mtime)
    size_bytes = path.stat().st_size

    if layout.kind == LAYOUT_JSON_ARRAY_INLINE:
        raw_blocks, blocks, lines = _inline_array_head(path, total, budget_bytes)
        line_count = first.line_count if first else count_lines(path)
        return _finish(raw_blocks, blocks, lines, size_bytes, line_count, mtime_iso, layout)

    head_b = int(budget_bytes * 0.70)
    mid_b = int(budget_bytes * 0.15)
    tail_b = budget_bytes - head_b - mid_b
    marker = layout.marker
    # Where the first record starts: byte 0, or after the array's opening bracket.
    head_off, head_line = 0, 1
    if layout.kind == LAYOUT_JSON_ARRAY:
        pos = probe.find(marker)
        head_off = pos + 1
        head_line = probe[: pos + 1].count(b"\n") + 1

    scan = _scan(
        path, marker, total // 2, max(total - tail_b, 0), max(total - 1, 0), quoted=layout.quoted
    )
    line_count = scan.line_count

    def take(label: str, offset: int, first_line: int, share: int) -> _Block:
        return _take_block(path, total, layout, label, offset, first_line, share)

    blocks_: list[_Block] = []
    if total <= budget_bytes:
        blocks_.append(take("head", head_off, head_line, budget_bytes))
    else:
        head = take("head", head_off, head_line, head_b)
        blocks_.append(head)
        # Every candidate below is clamped to what the head did not already
        # show: a file that is one enormous record has line_count == 1, and a
        # block that would repeat the head is dropped rather than duplicated.
        next_line, next_off = head_line + head.lines, head.next_off
        if scan.mid_line >= next_line:
            mid_line, mid_off = scan.mid_line, scan.mid_off
        else:
            mid_line, mid_off = next_line, next_off
        if mid_off is not None and mid_line <= line_count:
            mid = take("middle", mid_off, mid_line, mid_b)
            blocks_.append(mid)
            next_line, next_off = mid_line + mid.lines, mid.next_off
        # Tail: whole records that start inside the last ``tail_b`` bytes — or,
        # when none does (the last record is longer than that), the last record.
        tail_line, tail_off = scan.tail_line, scan.tail_off
        if tail_off is None:
            tail_line, tail_off = scan.last_line, scan.last_off
        if tail_line < next_line:
            tail_line, tail_off = next_line, next_off
        if tail_off is not None and tail_line <= line_count:
            blocks_.append(
                _take_block(path, total, layout, "tail", tail_off, tail_line, tail_b, keep_end=True)
            )

    raw_blocks = [(b.label, b.first, b.raw) for b in blocks_]
    blocks = [(b.label, b.first, b.shown) for b in blocks_]
    lines = [b.numbers for b in blocks_]
    return _finish(raw_blocks, blocks, lines, size_bytes, line_count, mtime_iso, layout)


@dataclass
class _Block:
    label: str
    first: int
    raw: str
    shown: str
    numbers: list[int]
    #: Lines of the file the raw records span, and where the record after them starts.
    lines: int
    next_off: int | None


#: How much a record reader pulls per read.
_READ_CHUNK = 1 << 16


class _RecordReader:
    """Whole records, one at a time, from one sequential read starting at ``offset``.

    One open per block — a ``.gz`` seek is a decompression from the start, so
    the excerpt never seeks per record. With quoted multi-line fields the
    boundary is the first marker with an even count of ``"`` since the block
    began (the block itself starts on such a boundary).
    """

    def __init__(self, path: Path, offset: int, total: int, layout: _Layout) -> None:
        self.fh = _open(path)
        self.fh.seek(offset)
        self.pos = offset
        self.total = total
        self.marker = layout.marker
        self.quoted = layout.quoted
        self.buf = b""
        self.eof = False
        self.quotes = 0

    def close(self) -> None:
        self.fh.close()

    def _fill(self) -> None:
        more = self.fh.read(_READ_CHUNK)
        if more:
            self.buf += more
        else:
            self.eof = True

    def next(self) -> tuple[bytes, int] | None:
        """``(record bytes, absolute start)`` without the marker; ``None`` at the end."""
        if not self.buf and not self.eof:
            self._fill()
        if not self.buf:
            return None
        start = 0
        while True:
            nl = _first_boundary(self.buf, self.marker, self.quoted, start, self.quotes)
            if nl >= 0:
                break
            if self.eof or len(self.buf) >= _MAX_RECORD_BYTES:
                nl = len(self.buf)  # the file's final record, or one at the cap
                break
            start = max(len(self.buf) - len(self.marker) + 1, 0)
            self._fill()
        rec, rec_start = self.buf[:nl], self.pos
        consumed = min(nl + 1, len(self.buf))  # the record after a marker starts one byte in
        self.buf = self.buf[consumed:]
        self.pos += consumed
        self.quotes += rec.count(b'"')
        return rec, rec_start


def _take_block(
    path: Path,
    total: int,
    layout: _Layout,
    label: str,
    offset: int,
    first_line: int,
    share: int,
    *,
    keep_end: bool = False,
) -> _Block:
    """Records from ``offset`` while what is *shown* fits ``share`` bytes — at least one.

    ``keep_end`` (the tail) reads to the end of the file and keeps the *last*
    records that fit, so the newest record is never the one dropped.
    """
    reader = _RecordReader(path, offset, total, layout)
    items: list[tuple[str, int, str, int]] = []  # (record, raw lines, shown, shown bytes)
    used = raw_len = 0
    next_off: int | None = None
    try:
        while True:
            item = reader.next()
            if item is None:
                break
            rec_b, rec_start = item
            rec = _strip_record(rec_b.decode("utf-8", errors="replace"), layout)
            shown = _show_record(rec, layout, share)
            size = len(shown.encode("utf-8")) + 1
            over = used + size > share or raw_len + len(rec_b) > _MAX_RECORD_BYTES
            if items and over and not keep_end:
                next_off = rec_start
                break
            items.append((rec, rec_b.count(b"\n") + 1, shown, size))
            used += size
            raw_len += len(rec_b)
    finally:
        reader.close()
    while keep_end and len(items) > 1 and used > share:
        _rec, lines, _shown, size = items.pop(0)
        first_line += lines
        used -= size
    numbers: list[int] = []
    lines = 0
    for _rec, rec_lines, shown, _size in items:
        numbers.extend(first_line + lines + j for j in range(shown.count("\n") + 1))
        lines += rec_lines
    if next_off is not None and next_off >= total:
        next_off = None
    joiner = ",\n" if layout.kind == LAYOUT_JSON_ARRAY else "\n"
    return _Block(
        label,
        first_line,
        joiner.join(i[0] for i in items),
        "\n".join(i[2] for i in items),
        numbers,
        lines,
        next_off,
    )


def _first_boundary(
    chunk: bytes, marker: bytes, quoted: bool, start: int, quotes_before: int = 0
) -> int:
    """Start of the first marker at or after ``start`` that ends a whole record (-1 if none)."""
    nearest = -1
    for _ in range(_QUOTE_STEPS):
        nl = chunk.find(marker, start)
        if nl < 0:
            break
        nearest = nl if nearest < 0 else nearest
        if not quoted or (quotes_before + chunk[: nl + 1].count(b'"')) % 2 == 0:
            return nl
        start = nl + 1
    return nearest


def _inline_array_head(
    path: Path, total: int, budget_bytes: int
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]], list[list[int]]]:
    """The first elements of a one-line array, decoded one by one."""
    with _open(path) as fh:
        text = fh.read(min(total, _MAX_RECORD_BYTES)).decode("utf-8", errors="replace")
    dec = json.JSONDecoder()
    pos = text.find("[") + 1
    raws: list[str] = []
    shown: list[str] = []
    used = 0
    layout = _Layout(LAYOUT_JSON_ARRAY_INLINE, b"")
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            break
        try:
            _obj, end = dec.raw_decode(text, pos)
        except ValueError:
            break
        raw = text[pos:end]
        s = _show_record(raw, layout, budget_bytes)
        if raws and used + len(s) + 1 > budget_bytes:
            break
        raws.append(raw)
        shown.append(s)
        used += len(s) + 1
        pos = end
    if not raws:  # not decodable element by element: show the head as text
        raws = [text[:budget_bytes]]
        shown = [_cut(text, budget_bytes)]
    head_line = text[: text.find("[") + 1].count("\n") + 1
    numbers = [head_line + i for i in range(sum(s.count("\n") + 1 for s in shown))]
    return (
        [("head", head_line, ",".join(raws))],
        [("head", head_line, "\n".join(shown))],
        [numbers],
    )


def _finish(
    raw_blocks: list[tuple[str, int, str]],
    blocks: list[tuple[str, int, str]],
    lines: list[list[int]],
    size_bytes: int,
    line_count: int,
    mtime_iso: str | None,
    layout: _Layout,
) -> Sample:
    text = "\n".join(b[2] for b in blocks)
    return Sample(
        blocks=blocks,
        raw_blocks=raw_blocks,
        record_lines=lines,
        text=text,
        size_bytes=size_bytes,
        line_count=line_count,
        mtime_iso=mtime_iso,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        layout=layout.kind,
    )


def safe_filename(name: str | None, default: str = "input.log") -> str:
    """Reduce a client-supplied filename to a plain basename.

    The multipart ``filename`` is attacker-controlled and is joined onto
    temporary directories (the sample-phase input, the regenerate copy), so it
    must never carry a directory component; ``..``/``.``/empty fall back to
    *default*.
    """
    base = Path((name or "").replace("\\", "/")).name.strip()
    if base in {"", ".", ".."}:
        return default
    return base


def sample_as_file(sample: Sample, dest_dir: Path, filename: str) -> Path:
    """Write the whole raw records as ``dest_dir/<basename(filename)>`` — the sample-phase input.

    Every block, so the guarded run sees a format that changed mid-file and the
    newest timestamps before the full run does. The file must look to the
    script exactly like the full file will: same name, the same delimiting
    (a top-level array is written back as one), and gzip bytes for a ``.gz``
    upload — so a converter that handles ``.gz`` by suffix behaves the same in
    both phases.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / safe_filename(filename)
    raws = [b[2] for b in sample.raw_blocks]
    if sample.layout == LAYOUT_JSON_ARRAY:
        body = "[\n" + ",\n".join(raws) + "\n]\n"
    elif sample.layout == LAYOUT_JSON_ARRAY_INLINE:
        body = "[" + raws[0] + "]\n"
    else:
        body = "\n".join(raws) + "\n"
    data = body.encode("utf-8")
    if out.suffix == ".gz":
        data = gzip.compress(data, mtime=0)
    out.write_bytes(data)
    return out
