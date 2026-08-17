"""Run a model-written converter with the guard the standard library affords.

No bwrap, no containers (decision 2026-08-17: no new system dependency). What
we do: an AST deny-list before anything runs, ``python -I`` (no user site, no
cwd on ``sys.path``), a private working directory, a scrubbed environment,
``RLIMIT_AS``/``RLIMIT_CPU``/``RLIMIT_FSIZE``/``RLIMIT_NOFILE``, a new session
so a timeout kills the whole group. What we do not do — and ``docs/DEPLOYMENT.md``
says so — is stop a script from writing anywhere the app user can write, or
from reaching the network if it evades the deny-list. Run the app as a
dedicated user.
"""

from __future__ import annotations

import ast
import contextlib
import os
import re
import resource
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DENIED_MODULES = frozenset(
    {
        "socket",
        "ssl",
        "subprocess",
        "multiprocessing",
        "concurrent",
        "ctypes",
        "http",
        "urllib",
        "xmlrpc",
        "ftplib",
        "smtplib",
        "asyncio",
        "importlib",
        "threading",
        "signal",
        "resource",
        "shutil",
        "tempfile",
        "_thread",
        "webbrowser",
        "pty",
    }
)
DENIED_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
DENIED_OS_ATTRS = frozenset(
    {
        "system",
        "popen",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "chmod",
        "chown",
        "posix_spawn",
        "posix_spawnp",
    }
)
_STDERR_TAIL = 4096
_PROGRESS_RE = re.compile(rb"^progress\s+(\d+)")


def check_script(script: str) -> list[str]:
    """Return violations (empty when the script may run). Best-effort static guard."""
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in DENIED_MODULES:
                    problems.append(f"line {node.lineno}: import of {a.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in DENIED_MODULES:
                problems.append(f"line {node.lineno}: import from {node.module!r} is not allowed")
            if root == "os":
                for a in node.names:
                    if a.name in DENIED_OS_ATTRS:
                        problems.append(f"line {node.lineno}: os.{a.name} is not allowed")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in DENIED_CALLS:
                problems.append(f"line {node.lineno}: call to {fn.id}() is not allowed")
            elif (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "os"
                and fn.attr in DENIED_OS_ATTRS
            ):
                problems.append(f"line {node.lineno}: os.{fn.attr}() is not allowed")
    return problems


@dataclass
class RunResult:
    """Outcome of one subprocess run."""

    exit_code: int | None
    elapsed_ms: int
    stderr_tail: str
    timed_out: bool
    killed_reason: str | None


def _preexec(memory_mb: int, cpu_s: int, output_mb: int) -> Callable[[], None]:
    def _apply() -> None:
        os.setsid()
        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        out = output_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (out, out))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        # No RLIMIT_NPROC: on Linux it counts the *user's* processes, so any
        # useful value starves pyarrow's BLAS thread pool on a busy host while
        # stopping nothing a deny-listed `os.fork` did not already stop.

    return _apply


def run_converter(
    script: str,
    input_path: Path,
    *,
    output_path: Path,
    timeout_s: float,
    memory_mb: int,
    output_mb: int,
    on_progress: Callable[[int], None] | None = None,
) -> RunResult:
    """Run ``script`` on ``input_path`` writing ``output_path``; blocking.

    ``on_progress(n)`` fires for each stderr line ``progress <n>``.
    """
    workdir = Path(tempfile.mkdtemp(prefix="vestigo-conv-"))
    try:
        script_path = workdir / "converter.py"
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o400)
        in_dir = workdir / "input"
        in_dir.mkdir()
        staged = in_dir / input_path.name
        try:
            os.link(input_path, staged)
        except OSError:
            shutil.copy2(input_path, staged)
        staged.chmod(0o400)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        env = {
            "PATH": os.path.dirname(sys.executable) + os.pathsep + "/usr/bin:/bin",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            # pyarrow pulls in numpy/OpenBLAS; one thread keeps the address
            # space inside RLIMIT_AS and the run single-process as promised.
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        cmd = [
            sys.executable,
            "-I",
            "-B",
            str(script_path),
            "-i",
            str(staged),
            "-o",
            str(output_path),
            "-v",
        ]
        started = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 — the whole point; guarded above
            cmd,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=_preexec(memory_mb, int(timeout_s), output_mb),
        )
        if proc.stderr is None:  # pragma: no cover — Popen(stderr=PIPE) guarantees it
            raise RuntimeError("stderr pipe missing")
        tail = bytearray()
        buf = b""
        timed_out = False
        deadline = started + timeout_s
        sel = selectors.DefaultSelector()
        sel.register(proc.stderr, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                events = sel.select(timeout=min(remaining, 0.5))
                if events:
                    chunk = os.read(proc.stderr.fileno(), 65536)
                    if not chunk:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        m = _PROGRESS_RE.match(line.strip())
                        if m and on_progress is not None:
                            on_progress(int(m.group(1)))
                        tail += line + b"\n"
                        if len(tail) > _STDERR_TAIL:
                            del tail[: len(tail) - _STDERR_TAIL]
                elif proc.poll() is not None:
                    break
        finally:
            sel.close()
        if timed_out:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, 9)
        exit_code = proc.wait(timeout=10)
        if buf:
            tail += buf
        elapsed = int((time.monotonic() - started) * 1000)
        return RunResult(
            exit_code=exit_code,
            elapsed_ms=elapsed,
            stderr_tail=bytes(tail[-_STDERR_TAIL:]).decode("utf-8", errors="replace"),
            timed_out=timed_out,
            killed_reason="timeout" if timed_out else None,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
