"""Bounded excerpt of a raw text log for the model, plus text/binary detection.

Head takes 70% of the budget, a middle window 15%, the tail 15% — formats change
mid-file and the tail shows the newest timestamps. Whole lines only, absolute
line numbers, so the model can cite them. ``.gz`` is read transparently.

The excerpt teaches the model a *format*, not a story, and a log is mostly the
same handful of lines over and over: 3006 lines of one nginx access log were
~50k tokens of prompt, of which about fifteen distinct line shapes carried every
fact the converter needed. So each block is condensed — at most
:data:`CONDENSE_KEEP_PER_SHAPE` lines per distinct shape — and, to spend the
budget on *variety* rather than repetition, each window reads
:data:`CONDENSE_SCAN_FACTOR` times its share off disk before condensing. Elided
lines leave a gap in the numbering, which the renderer marks; nothing is
reordered and no line is ever rewritten.
"""

from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

_PROBE_BYTES = 8192

#: Bytes read per window before condensing, as a multiple of that window's
#: share of the budget. A repetitive file needs several times the budget on
#: disk to yield a budget's worth of distinct lines; the windows are clamped
#: against each other so a bigger read never swallows the middle or the tail.
CONDENSE_SCAN_FACTOR = 4

#: Lines kept per distinct shape, per block. Five is enough to show which parts
#: of a shape vary (paths, status codes, hosts) without paying for the sixth.
CONDENSE_KEEP_PER_SHAPE = 5

_INDENT = (" ", "\t")


class NotTextError(ValueError):
    """The file does not look like text (empty, NUL bytes, or mostly non-printable)."""


@dataclass(frozen=True)
class Block:
    """One labelled window of the excerpt: its lines with their absolute numbers.

    Condensing removes lines, so the numbers are not contiguous — they are the
    line's own position in the file, which is what the model is asked to cite.
    """

    label: str
    #: ``(absolute 1-based line number, line text)``, in file order.
    lines: list[tuple[int, str]]

    @property
    def first(self) -> int:
        """Absolute number of the block's first line (1 when the block is empty)."""
        return self.lines[0][0] if self.lines else 1

    @property
    def text(self) -> str:
        """The kept lines, newline-joined — what the model is shown for this block."""
        return "\n".join(t for _n, t in self.lines)


@dataclass(frozen=True)
class Sample:
    """The excerpt sent to the model and the facts the task header states."""

    blocks: list[Block]
    text: str
    size_bytes: int
    line_count: int
    #: The evidence file's own mtime as ISO-8601 UTC, or ``None`` when the
    #: caller could not learn it (a raw API upload carries none) — never the
    #: staging copy's, which is just the upload time.
    mtime_iso: str | None
    sha256: str


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


_SCAN_CHUNK = 1 << 20


class _Threshold:
    """What the excerpt needs to know about one byte position ``at`` in the stream.

    ``newlines_before``: count of ``\\n`` in ``data[:at]``; ``last_nl_before``:
    position of the last of them (-1 if none); ``first_nl_from``: position of
    the first ``\\n`` at or after ``at`` (``None`` if none). Fed chunk by chunk.
    """

    def __init__(self, at: int) -> None:
        self.at = at
        self.newlines_before = 0
        self.last_nl_before = -1
        self.first_nl_from: int | None = None

    def feed(self, chunk: bytes, base: int) -> None:
        end = base + len(chunk)
        if base < self.at:
            head = chunk[: self.at - base]
            self.newlines_before += head.count(b"\n")
            nl = head.rfind(b"\n")
            if nl >= 0:
                self.last_nl_before = base + nl
        if end > self.at and self.first_nl_from is None:
            nl = chunk.find(b"\n", max(self.at - base, 0))
            if nl >= 0:
                self.first_nl_from = base + nl


@dataclass
class _Scan:
    """What one streaming pass learns about the file (O(1) memory)."""

    total: int
    line_count: int
    #: ``middle``: index of the line containing byte M and its start offset.
    mid_idx: int
    mid_off: int
    #: ``tail``: index of the first line starting at or after byte T and its
    #: start offset (``None`` when no line starts there).
    tail_idx: int
    tail_off: int | None


def _scan(path: Path, mid_at: int | None = None, tail_at: int | None = None) -> _Scan:
    """Count lines in fixed-size chunks and resolve the two byte thresholds.

    Replaces a per-line offset list — 100M lines meant ~4 GB of ints in the API
    process for a file at the upload cap. Everything the excerpt needs from the
    whole file is its length, its line count and two (index, offset) pairs, so
    that is all this keeps. The thresholds may be omitted when the length is
    not known up front (a ``.gz``): the caller scans once for the length and
    once more with the thresholds.
    """
    total = 0
    newlines = 0
    last_byte = b""
    mid = _Threshold(mid_at) if mid_at is not None else None
    # A line starts at or after T iff its newline sits at or after T-1.
    tail = _Threshold(max(tail_at - 1, 0)) if tail_at is not None else None
    with _open(path) as fh:
        while chunk := fh.read(_SCAN_CHUNK):
            if mid is not None:
                mid.feed(chunk, total)
            if tail is not None:
                tail.feed(chunk, total)
            newlines += chunk.count(b"\n")
            total += len(chunk)
            last_byte = chunk[-1:]
    line_count = newlines + (1 if last_byte and last_byte != b"\n" else 0)
    mid_idx = mid.newlines_before if mid else 0
    mid_off = mid.last_nl_before + 1 if mid else 0
    tail_idx = tail_off = 0
    if tail is not None:
        tail_idx = tail.newlines_before + 1 if tail_at else 0
        tail_off = tail.first_nl_from + 1 if tail.first_nl_from is not None else None
        if tail_at == 0:
            tail_off = 0
        elif tail_off is not None and tail_off >= total:
            tail_off = None  # that newline was the file's last byte: no line starts there
    return _Scan(total, line_count, mid_idx, mid_off, tail_idx, tail_off)


def count_lines(path: Path) -> int:
    """Line count of a (possibly gzipped) text file, streamed."""
    return _scan(path).line_count


def mtime_to_iso(mtime: float | None) -> str | None:
    """``None`` stays ``None``; a POSIX timestamp becomes ``YYYY-MM-DDTHH:MM:SSZ``."""
    if mtime is None:
        return None
    return datetime.fromtimestamp(mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'")

#: What makes a quoted run *not* ordinary text: an escape sequence the writer
#: emitted for a byte it could not print, or a control character itself.
_ODD_BYTES = re.compile(r"\\x[0-9a-fA-F]{2}|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _mask_quoted(match: re.Match[str]) -> str:
    """Collapse a quoted run to its *kind*, not its content.

    A quoted field's content is payload — the request line, the user agent —
    and masking it is what lets a thousand different URLs count as one shape.
    But three kinds of quoted run break parsers differently and must stay
    distinct: empty, ordinary text, and a run carrying escaped or control
    bytes (a TLS handshake sent to an HTTP port arrives exactly this way).
    """
    quote = match.group(0)[0]
    body = match.group(0)[1:-1]
    if not body:
        return quote * 2
    return f"{quote}{'B' if _ODD_BYTES.search(body) else 'S'}{quote}"


#: Applied after quoting: long hex tokens (hashes, ids), then any run of
#: digits, then whitespace runs. Two lines with the same shape teach the model
#: the same thing about the format.
_SHAPE_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b[0-9a-fA-F]{6,}\b"), "H"),
    (re.compile(r"\d+"), "N"),
    (re.compile(r"[ \t]+"), " "),
)

#: Only this many characters of a line take part in its shape — a 10 MB
#: minified JSON line must not cost 10 MB of regex work per line.
_SHAPE_PREFIX_CHARS = 400


def _shape(line: str) -> str:
    """The line with its variable parts masked: quoted runs, hex ids, digits."""
    shape = _QUOTED.sub(_mask_quoted, line[:_SHAPE_PREFIX_CHARS])
    for pattern, repl in _SHAPE_SUBS:
        shape = pattern.sub(repl, shape)
    return shape


def _drop_repeats(lines: list[tuple[int, str]], *, from_end: bool) -> list[tuple[int, str]]:
    """Keep :data:`CONDENSE_KEEP_PER_SHAPE` lines of each shape, per block.

    ``from_end`` walks the block backwards and so keeps the *last* of each
    shape — what the tail is for: the newest timestamps, and the tail block
    must still end at EOF.

    A line that is indented, or whose neighbour on the walking side is, is
    always kept: that is how a multi-line record (a stack trace, a wrapped
    message) looks, and dropping a parent line would leave its continuation
    attached to something else.
    """
    order = list(reversed(lines)) if from_end else lines
    kept: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    for i, (no, text) in enumerate(order):
        neighbour = order[i + 1][1] if i + 1 < len(order) else ""
        shape = _shape(text)
        n = seen.get(shape, 0)
        seen[shape] = n + 1
        part_of_record = text[:1] in _INDENT or neighbour[:1] in _INDENT
        if n >= CONDENSE_KEEP_PER_SHAPE and not part_of_record:
            continue
        kept.append((no, text))
    return list(reversed(kept)) if from_end else kept


def _cap_bytes(lines: list[tuple[int, str]], cap: int, *, from_end: bool) -> list[tuple[int, str]]:
    """Trim to ``cap`` bytes of text. The tail trims from the front — it ends at EOF.

    One line longer than the whole cap is sliced rather than dropped, so a block
    is never empty and the disclosed size is never blown.
    """
    seq = list(reversed(lines)) if from_end else lines
    out: list[tuple[int, str]] = []
    used = 0
    for no, text in seq:
        cost = len(text.encode()) + 1
        if not out and cost > cap:
            sliced = text.encode()[: max(cap - 1, 0)].decode("utf-8", errors="ignore")
            out.append((no, sliced))
            break
        if used + cost > cap:
            break
        out.append((no, text))
        used += cost
    return list(reversed(out)) if from_end else out


#: Each block's share of the budget, mirroring the read split.
_SHARES = {"head": 0.70, "middle": 0.15, "tail": 0.15}


def _condense(blocks: list[Block], budget_bytes: int) -> list[Block]:
    """Drop repeated shapes from every block, then trim each to its share."""
    out: list[Block] = []
    for block in blocks:
        cap = budget_bytes if len(blocks) == 1 else max(int(budget_bytes * _SHARES[block.label]), 1)
        from_end = block.label == "tail"
        lines = _drop_repeats(block.lines, from_end=from_end)
        out.append(Block(block.label, _cap_bytes(lines, cap, from_end=from_end)))
    return out


def _numbered(first: int, text: str) -> list[tuple[int, str]]:
    return [(first + i, line) for i, line in enumerate(text.split("\n"))]


def build_sample(
    path: Path, budget_bytes: int, *, mtime: float | None = None, condense: bool = True
) -> Sample:
    """Return the excerpt sent to the model; raises :class:`NotTextError` for binary.

    ``mtime`` is the evidence file's modification time as the uploader knew it
    (``File.lastModified`` in the browser, ``stat`` on the CLI). It is *not*
    read from ``path``: that is a staging copy whose mtime is the upload time,
    and the prompt tells the model to take a missing year from the mtime.

    ``condense=False`` returns the raw windows — every line of them, contiguous
    — which is what the streaming reader is tested against. A file that fits in
    the budget is returned whole either way.
    """
    assert_text_file(path)
    factor = CONDENSE_SCAN_FACTOR if condense else 1
    head_b = int(budget_bytes * 0.70) * factor
    mid_b = int(budget_bytes * 0.15) * factor
    tail_b = (budget_bytes - int(budget_bytes * 0.70) - int(budget_bytes * 0.15)) * factor
    # The thresholds depend on the decompressed length, which only a pass over
    # a ``.gz`` reveals; a plain file's length is its size on disk.
    first = _scan(path) if path.suffix == ".gz" else None
    total = first.total if first else path.stat().st_size
    if total > budget_bytes:
        mid_at = total // 2
        if condense:
            # A window enlarged by the scan factor must not swallow the next
            # one: the middle and the tail are why the excerpt has three blocks.
            head_b = min(head_b, mid_at)
            mid_b = min(mid_b, max(total - tail_b - mid_at, 0))
        scan = _scan(path, mid_at, max(total - tail_b, 0))
        line_count = scan.line_count
    else:
        scan = None
        line_count = first.line_count if first else count_lines(path)
    mtime_iso = mtime_to_iso(mtime)

    def read_lines(offset: int, byte_budget: int) -> tuple[str, int | None]:
        """Whole lines from ``offset``: the text and where the line after them starts.

        The second value is ``None`` when no line follows. A block that had to
        stop inside a line (one longer than its budget) still hands the next
        block the *next line's* start, found by streaming forward — the
        excerpt never repeats a line and never begins mid-line.
        """
        with _open(path) as fh:
            fh.seek(offset)
            chunk = fh.read(byte_budget)
            # Whole lines only: drop a trailing partial line unless it is the file's last.
            if not chunk.endswith(b"\n") and offset + len(chunk) < total and b"\n" in chunk:
                chunk = chunk[: chunk.rfind(b"\n") + 1]
            end: int | None = offset + len(chunk)
            if not chunk.endswith(b"\n"):
                # Mid-line: the next line starts after the next newline, if any.
                pos = end
                end = None
                while more := fh.read(_SCAN_CHUNK):
                    nl = more.find(b"\n")
                    if nl >= 0:
                        end = pos + nl + 1
                        break
                    pos += len(more)
                if end is not None and end >= total:
                    end = None
        return chunk.decode("utf-8", errors="replace").rstrip("\n"), end

    if scan is None:
        blocks = [Block("head", _numbered(1, read_lines(0, total)[0] if line_count else ""))]
    else:
        head_text, head_end = read_lines(0, head_b)
        head_lines = head_text.count("\n") + 1
        blocks = [Block("head", _numbered(1, head_text))]
        # Every index below is clamped to the lines that exist: a file that is
        # one enormous line (minified JSON, CR-only endings) has line_count == 1
        # and must not index past it. Blocks that would repeat the head are
        # dropped rather than duplicated.
        next_idx, next_off = head_lines, head_end
        if scan.mid_idx >= head_lines:
            mid_idx, mid_off = scan.mid_idx, scan.mid_off
        else:
            mid_idx, mid_off = head_lines, head_end
        if mid_idx < line_count and mid_off is not None:
            mid_text, mid_end = read_lines(mid_off, mid_b)
            blocks.append(Block("middle", _numbered(mid_idx + 1, mid_text)))
            next_idx, next_off = mid_idx + mid_text.count("\n") + 1, mid_end
        # Tail: whole lines that start inside the last ``tail_b`` bytes, capped
        # at the budget so one huge last line cannot blow the disclosed size.
        if scan.tail_idx >= next_idx:
            tail_idx, tail_off = scan.tail_idx, scan.tail_off
        else:
            tail_idx, tail_off = next_idx, next_off
        if tail_idx < line_count and tail_off is not None:
            tail_text, _ = read_lines(tail_off, min(total - tail_off, tail_b))
            blocks.append(Block("tail", _numbered(tail_idx + 1, tail_text)))
    # A file that already fits in the budget is shown whole: condensing it
    # would throw information away and buy nothing.
    if condense and total > budget_bytes:
        blocks = _condense(blocks, budget_bytes)
    text = "\n".join(b.text for b in blocks)
    return Sample(
        blocks=blocks,
        text=text,
        size_bytes=path.stat().st_size,
        line_count=line_count,
        mtime_iso=mtime_iso,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
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
    """Write the head block as ``dest_dir/<basename(filename)>`` — the sample-phase input.

    Every block, not just the head: condensing has already removed the repeats,
    so the whole excerpt is small, and running the script over the middle and
    the tail as well is how the sample phase gets to see the lines that break
    parsers — an empty request, a binary handshake, a format that changed
    halfway through the file.

    The sample file must look to the script exactly like the full file will:
    same name and, for a ``.gz`` upload, gzip bytes — so a converter that
    handles ``.gz`` by suffix behaves the same in both phases.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / safe_filename(filename)
    data = (sample.text + "\n").encode("utf-8")
    if out.suffix == ".gz":
        data = gzip.compress(data, mtime=0)
    out.write_bytes(data)
    return out
