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
        "imaplib",
        "poplib",
        "nntplib",
        "telnetlib",
        "socketserver",
        "wsgiref",
        "asyncio",
        "importlib",
        "threading",
        "signal",
        "resource",
        "shutil",
        "tempfile",
        "_thread",
        "webbrowser",
        "antigravity",
        "pty",
        # Ways back to the interpreter's own machinery: import-by-string,
        # module runners and loaders, the builtins namespace, the debugger
        # REPL, and the object graph (``gc``/``inspect`` hand out frames and
        # globals of anything alive).
        "builtins",
        "runpy",
        "code",
        "codeop",
        "pdb",
        "pydoc",
        "pkgutil",
        "zipimport",
        "ensurepip",
        "venv",
        "site",
        "gc",
        "inspect",
        "unittest",
        # Deserialisers that build arbitrary objects (and so call arbitrary
        # callables) from bytes.
        "pickle",
        "_pickle",
        "marshal",
        "shelve",
        "dbm",
        # The private modules the public ones above are built on — a script that
        # imports ``_socket`` or ``_posixsubprocess`` directly gets the same answer.
        "posix",
        "_socket",
        "_ssl",
        "_posixsubprocess",
        "_multiprocessing",
        "_ctypes",
    }
)
#: Submodules refused although their package is allowed: ``logging.config``
#: resolves ``()`` factory strings by importing them, ``unittest.mock`` patches
#: whatever it is pointed at.
DENIED_SUBMODULES = frozenset({"logging.config", "unittest.mock"})
#: What a converter may import at all: the standard library (minus the
#: deny-list) and the columnar stack the contract is written against. This is
#: the prompt's own "pyarrow + stdlib" promise, enforced — a third-party
#: import the operator never installed is rejected before the script runs.
ALLOWED_THIRD_PARTY = frozenset({"pyarrow", "numpy"})
#: Builtins refused wherever they are *named* — as a call, as a value
#: (``f = eval; f(...)``), as a from-import. ``globals``/``locals``/``vars``
#: hand back namespaces that hold every module alias the script imported.
DENIED_CALLS = frozenset({"exec", "eval", "compile", "__import__"})
DENIED_NAMES = frozenset(
    DENIED_CALLS
    | {
        "globals",
        "locals",
        "vars",
        "breakpoint",
        "help",
        "input",
        "__builtins__",
        "__loader__",
        "__spec__",
    }
)
#: The reflection family: refused on a module alias (``getattr(os, name)``) and
#: with a dunder-name string argument on anything.
REFLECTION_CALLS = frozenset(
    {"getattr", "setattr", "delattr", "hasattr", "attrgetter", "methodcaller"}
)
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
#: Denied as an attribute on *any* receiver, read or called: none of these has
#: a benign homonym a converter would use on a string, list or table (unlike
#: ``replace``/``remove``), ``pathlib.Path`` exposes several of them without
#: ``os`` ever appearing in the source, and ``types.CodeType``/``FunctionType``
#: build callables from raw bytecode.
DENIED_ANY_ATTRS = frozenset(
    DENIED_OS_ATTRS - {"remove", "rename", "renames", "replace"}
    | {"lchmod", "lchown", "CodeType", "FunctionType", "get_type_hints"}
)
#: Dunder attributes that walk from any object back to the builtins, the
#: import machinery, a function's globals or a frame — refused on every
#: receiver. ``__class__``/``__name__``/``__doc__`` stay allowed; the paths
#: onward from a class (``__bases__``, ``__mro__``, ``__subclasses__``,
#: ``__dict__``) are what is closed.
DENIED_DUNDER_ATTRS = frozenset(
    {
        "__dict__",
        "__builtins__",
        "__loader__",
        "__spec__",
        "__import__",
        "__subclasses__",
        "__bases__",
        "__base__",
        "__mro__",
        "__globals__",
        "__code__",
        "__closure__",
        "__self__",
        "__func__",
        "__wrapped__",
        "__reduce__",
        "__reduce_ex__",
        "__getattribute__",
        "__getattr__",
        "__init_subclass__",
        "__traceback__",
        "tb_frame",
        "tb_next",
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_back",
        "gi_frame",
        "cr_frame",
        "ag_frame",
    }
)
#: Attribute reads on ``sys`` that hand back the import machinery or a frame,
#: or let a written file shadow a stdlib module on the next import.
DENIED_SYS_ATTRS = frozenset(
    {
        "modules",
        "path",
        "meta_path",
        "path_hooks",
        "path_importer_cache",
        "_getframe",
        "_current_frames",
        "settrace",
        "setprofile",
        "call_tracing",
        "addaudithook",
    }
)
_STDERR_TAIL = 4096
_PROGRESS_RE = re.compile(rb"^progress\s+(\d+)")
_DUNDER_RE = re.compile(r"^__.*__$")


def _import_allowed(root: str) -> bool:
    return root not in DENIED_MODULES and (
        root in sys.stdlib_module_names or root in ALLOWED_THIRD_PARTY
    )


def _module_denied(dotted: str) -> bool:
    root = dotted.split(".")[0]
    if not _import_allowed(root):
        return True
    return any(dotted == d or dotted.startswith(d + ".") for d in DENIED_SUBMODULES)


def _dunder_string_arg(node: ast.Call) -> bool:
    return any(
        isinstance(a, ast.Constant) and isinstance(a.value, str) and _DUNDER_RE.match(a.value)
        for a in [*node.args, *(k.value for k in node.keywords)]
    )


def check_script(script: str) -> list[str]:
    """Return violations (empty when the script may run). Best-effort static guard.

    Imports are allow-listed (stdlib minus the deny-list, plus pyarrow/numpy)
    and their aliases resolved, so ``import os as o; o.system(...)`` reads as
    ``os.system``; ``from os import *`` is refused outright since it makes every
    later bare name unresolvable. A module bound by ``import`` may only ever be
    the left side of an attribute access — ``x = os``, ``f(os)``, ``[os]`` are
    refused, so a denied attribute cannot be reached through a rebinding — and
    the dunder attributes that walk from any object back to the builtins or a
    frame are refused on every receiver. Still static — a determined script
    can look for another path through the object graph; the runner's rlimits,
    the private input copy and the dedicated app user are what stand behind
    this, and ``docs/DEPLOYMENT.md`` says so.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return [f"syntax error at line {exc.lineno}: {exc.msg}"]
    problems: list[str] = []
    aliases: dict[str, str] = {}  # local name -> module root it is bound to
    modules: set[str] = set()  # local names bound by ``import`` (a module object for sure)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if _module_denied(a.name):
                    problems.append(f"line {node.lineno}: import of {a.name!r} is not allowed")
                local = a.asname or root
                aliases[local] = root
                modules.add(local)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level:
                problems.append(f"line {node.lineno}: relative imports are not allowed")
                continue
            if _module_denied(node.module or ""):
                problems.append(f"line {node.lineno}: import from {node.module!r} is not allowed")
            for a in node.names:
                if a.name == "*":
                    problems.append(f"line {node.lineno}: 'from {root} import *' is not allowed")
                elif f"{node.module}.{a.name}" in DENIED_SUBMODULES:
                    problems.append(
                        f"line {node.lineno}: import of '{node.module}.{a.name}' is not allowed"
                    )
                elif root == "os" and a.name in DENIED_OS_ATTRS:
                    problems.append(f"line {node.lineno}: os.{a.name} is not allowed")
                elif root == "sys" and a.name in DENIED_SYS_ATTRS:
                    problems.append(f"line {node.lineno}: sys.{a.name} is not allowed")
                elif (
                    a.name in DENIED_NAMES
                    or a.name in DENIED_ANY_ATTRS
                    or a.name in DENIED_DUNDER_ATTRS
                    or a.name.startswith("__")
                ):
                    problems.append(f"line {node.lineno}: importing {a.name!r} is not allowed")
                elif "." not in (node.module or ""):
                    # ``from os import path as p`` binds a submodule; remember it
                    # so ``p.<attr>`` resolves back to ``os``.
                    aliases[a.asname or a.name] = root
    # Second pass with parent links: a module alias is only ever allowed as the
    # receiver of an attribute access.
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in DENIED_NAMES:
                problems.append(f"line {node.lineno}: {node.id} is not allowed")
            elif node.id in modules and isinstance(node.ctx, ast.Load):
                parent = parents.get(node)
                if not (isinstance(parent, ast.Attribute) and parent.value is node):
                    problems.append(
                        f"line {node.lineno}: module {node.id!r} may only be used as "
                        f"'{node.id}.<attribute>'"
                    )
        elif isinstance(node, ast.Attribute):
            root = aliases.get(node.value.id) if isinstance(node.value, ast.Name) else None
            if node.attr in DENIED_DUNDER_ATTRS:
                problems.append(f"line {node.lineno}: .{node.attr} is not allowed")
            elif root == "sys" and node.attr in DENIED_SYS_ATTRS:
                problems.append(f"line {node.lineno}: sys.{node.attr} is not allowed")
            elif root == "os" and node.attr in DENIED_OS_ATTRS:
                problems.append(f"line {node.lineno}: os.{node.attr} is not allowed")
            elif node.attr in DENIED_ANY_ATTRS:
                problems.append(f"line {node.lineno}: .{node.attr} is not allowed")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name in REFLECTION_CALLS:
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases:
                    problems.append(
                        f"line {node.lineno}: {name}() on module {node.args[0].id!r} is not allowed"
                    )
                elif _dunder_string_arg(node):
                    problems.append(
                        f"line {node.lineno}: {name}() with a dunder name is not allowed"
                    )
    return problems


@dataclass
class RunResult:
    """Outcome of one subprocess run."""

    exit_code: int | None
    elapsed_ms: int
    stderr_tail: str
    timed_out: bool
    killed_reason: str | None


def _bootstrap(memory_mb: int, cpu_s: int, output_mb: int) -> str:
    """Source of the ``-c`` stub that applies the rlimits *inside* the child, then execs.

    Not ``preexec_fn``: that runs Python between fork and exec in a process
    that may hold another thread's locks — documented unsafe, and this runner
    is called from ``asyncio.to_thread`` inside a threaded uvicorn. The stub
    runs after exec in a clean interpreter, sets the limits on itself, and
    execs the real command; limits survive ``execv``. ``start_new_session``
    on the ``Popen`` puts the whole tree in one killable group.
    """
    mem = memory_mb * 1024 * 1024
    out = output_mb * 1024 * 1024
    return (
        "import os, resource, sys\n"
        f"resource.setrlimit(resource.RLIMIT_AS, ({mem}, {mem}))\n"
        f"resource.setrlimit(resource.RLIMIT_CPU, ({cpu_s}, {cpu_s + 5}))\n"
        f"resource.setrlimit(resource.RLIMIT_FSIZE, ({out}, {out}))\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))\n"
        # No RLIMIT_NPROC: on Linux it counts the *user's* processes, so any
        # useful value starves pyarrow's BLAS thread pool on a busy host while
        # stopping nothing a deny-listed `os.fork` did not already stop.
        "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n"
    )


def run_converter(
    script: str,
    input_path: Path,
    *,
    output_path: Path,
    timeout_s: float,
    memory_mb: int,
    output_mb: int,
    on_progress: Callable[[int], None] | None = None,
    input_name: str | None = None,
    input_mtime: float | None = None,
) -> RunResult:
    """Run ``script`` on ``input_path`` writing ``output_path``; blocking.

    The input is staged read-only under ``input_name`` (default: the path's own
    basename) — pass the evidence file's real name when ``input_path`` is a
    content-addressed retention copy, so the script's ``-i`` and therefore the
    ``source_file``/``original_files`` it records name the evidence, not a hash.
    ``input_mtime`` (POSIX seconds) is stamped onto the staged copy for the
    same reason: the ``original_files[].mtime`` a script records, and the year
    it may take from the mtime, must be the evidence file's, not the upload's.
    ``on_progress(n)`` fires for each stderr line ``progress <n>``.
    """
    workdir = Path(tempfile.mkdtemp(prefix="vestigo-conv-"))
    try:
        script_path = workdir / "converter.py"
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(0o400)
        in_dir = workdir / "input"
        in_dir.mkdir()
        staged = in_dir / Path(input_name or input_path.name).name
        # A private *copy*, never a hardlink: the full run stages the
        # content-addressed retention copy of the evidence, and a link would
        # share its inode — the chmod below would land on the retained file
        # and a script that opens its input for append (or chmods it back)
        # would rewrite the evidence under its own hash. Copying a large log
        # once per run is the price of that guarantee.
        shutil.copyfile(input_path, staged)
        if input_mtime is not None:
            os.utime(staged, (input_mtime, input_mtime))
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
            "-c",
            _bootstrap(memory_mb, int(timeout_s), output_mb),
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
            start_new_session=True,
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
                    if len(buf) > _STDERR_TAIL:
                        # A partial line is not covered by RLIMIT_FSIZE (pipes
                        # are not files); keep only what the tail could show.
                        buf = buf[-_STDERR_TAIL:]
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
        if not timed_out:
            # stderr hit EOF (or the process exited): wait out the *remaining*
            # deadline, not a fixed grace — a script that closed its stderr and
            # kept running must still be reaped, and killed, on the same clock.
            try:
                proc.wait(timeout=max(deadline - time.monotonic(), 0.0))
            except subprocess.TimeoutExpired:
                timed_out = True
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
