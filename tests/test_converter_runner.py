"""AST deny-list, rlimits, timeout, env scrub — the whole stdlib guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from vestigo.converters import prompt as P
from vestigo.converters.runner import DENIED_MODULES, check_script, run_converter

GOOD = "import argparse, hashlib, gzip, os, re, sys, json\nfrom pathlib import Path\nimport pyarrow as pa\n"


@pytest.mark.parametrize(
    "bad",
    [
        "import socket",
        "from subprocess import run",
        "import multiprocessing",
        "import ctypes",
        "import urllib.request",
        "import importlib",
        "import threading",
        "import shutil",
        "os.system('ls')",
        "exec('1')",
        "eval('1')",
        "__import__('os')",
        "os.remove('x')",
        "os.popen('x')",
        "from os import unlink",
    ],
)
def test_check_script_rejects(bad):
    assert check_script(GOOD + bad + "\n"), bad


def test_check_script_accepts_fixture():
    src = Path("tests/fixtures/converters/syslog_fixture_converter.py").read_text()
    assert check_script(src) == []


def test_check_script_reports_syntax_error():
    assert any("syntax" in v.lower() for v in check_script("def (:\n"))


def test_prompt_and_runner_agree_on_denied_modules():
    assert set(P.DENIED_MODULES) <= DENIED_MODULES


def _run(tmp_path, script, timeout=20, memory_mb=2048, output_mb=64, on_progress=None):
    inp = tmp_path / "in.log"
    inp.write_text("x\n")
    return run_converter(
        script,
        inp,
        output_path=tmp_path / "out" / "out.parquet",
        timeout_s=timeout,
        memory_mb=memory_mb,
        output_mb=output_mb,
        on_progress=on_progress,
    )


def test_env_is_scrubbed_and_cwd_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_SECRET_THING", "1")
    monkeypatch.setenv("HTTP_PROXY", "http://x")
    script = (
        "import os, sys, json\n"
        "print(json.dumps({'env': sorted(os.environ), 'path': sys.path[:2]}), file=sys.stderr)\n"
    )
    r = _run(tmp_path, script)
    assert r.exit_code == 0, r.stderr_tail
    assert "VESTIGO_SECRET_THING" not in r.stderr_tail and "HTTP_PROXY" not in r.stderr_tail
    assert '""' not in r.stderr_tail  # -I: no '' (cwd) on sys.path


def test_timeout_kills_process_group(tmp_path):
    r = _run(tmp_path, "import time\ntime.sleep(30)\n", timeout=2)
    assert r.timed_out and r.exit_code != 0 and r.killed_reason == "timeout"
    assert r.elapsed_ms < 10_000


def test_memory_limit(tmp_path):
    r = _run(tmp_path, "x = bytearray(3 * 1024 * 1024 * 1024)\n", memory_mb=2048)
    assert r.exit_code != 0 and "MemoryError" in r.stderr_tail


def test_output_size_limit(tmp_path):
    script = (
        "import sys\nout = sys.argv[sys.argv.index('-o') + 1]\n"
        "with open(out, 'wb') as f:\n    f.write(b'0' * (70 * 1024 * 1024))\n"
    )
    r = _run(tmp_path, script, output_mb=64)
    assert r.exit_code != 0


def test_progress_lines_are_forwarded(tmp_path):
    seen: list[int] = []
    script = (
        "import sys\nprint('progress 5', file=sys.stderr)\nprint('progress 9', file=sys.stderr)\n"
    )
    r = _run(tmp_path, script, on_progress=seen.append)
    assert r.exit_code == 0 and seen == [5, 9]


def test_pyarrow_importable_under_limits(tmp_path):
    r = _run(tmp_path, "import pyarrow.parquet as pq\nprint('ok')\n")
    assert r.exit_code == 0, r.stderr_tail


def test_input_is_staged_read_only_and_args_are_passed(tmp_path):
    script = (
        "import sys, os\n"
        "i = sys.argv[sys.argv.index('-i') + 1]\n"
        "assert os.path.basename(i) == 'in.log', i\n"
        "assert open(i).read() == 'x\\n'\n"
        "assert not os.access(i, os.W_OK)\n"
        "assert '-v' in sys.argv\n"
    )
    r = _run(tmp_path, script)
    assert r.exit_code == 0, r.stderr_tail


def test_closed_stderr_does_not_orphan_a_running_child(tmp_path):
    # EOF on the stderr pipe while the child keeps running must still hit the
    # deadline and kill the group — not escape as an uncaught TimeoutExpired.
    r = _run(tmp_path, "import os, time\nos.close(2)\ntime.sleep(30)\n", timeout=2)
    assert r.timed_out and r.exit_code != 0 and r.killed_reason == "timeout"
    assert r.elapsed_ms < 10_000


def test_input_is_staged_under_the_given_name(tmp_path):
    # A retention copy is named by hash; the script must still see the evidence name.
    inp = tmp_path / ("a" * 64)
    inp.write_text("x\n")
    script = (
        "import sys, os\n"
        "i = sys.argv[sys.argv.index('-i') + 1]\n"
        "assert os.path.basename(i) == 'auth.log.gz', i\n"
    )
    r = run_converter(
        script,
        inp,
        output_path=tmp_path / "out" / "out.parquet",
        timeout_s=20,
        memory_mb=2048,
        output_mb=64,
        input_name="../auth.log.gz",
    )
    assert r.exit_code == 0, r.stderr_tail


def test_private_network_modules_are_denied():
    for mod in ("_socket", "_posixsubprocess", "posix"):
        assert check_script(f"import {mod}\n"), mod


@pytest.mark.parametrize(
    "bad",
    [
        "import os as o\no.system('id')",
        "from os import *\nsystem('id')",
        "import os as o\nfrom o import unlink",
        "import builtins\nbuiltins.__import__('socket')",
        "import sys\nsys.modules['os'].system('id')",
        "import sys as s\ns.modules['os']",
        "import runpy",
        "import pandas",
        "from urllib import request as r",
        "import os as o\ngetattr(o, 'sys' + 'tem')('id')",
        "from pathlib import Path\nPath('x').chmod(0o644)",
        "from pathlib import Path\nPath('x').unlink()",
        "import os.path as p\nfrom os import path\nos.chmod('x', 0)",
    ],
)
def test_check_script_rejects_evasions(bad):
    assert check_script(GOOD + bad + "\n"), bad


def test_check_script_allows_stdlib_and_pyarrow_aliases():
    ok = (
        "import argparse as ap, json as j\nimport pyarrow.parquet as pq\nimport numpy as np\n"
        "from datetime import datetime as dt\nfrom os import path as osp\n"
        "s = 'a'.replace('a', 'b'); l = [1]; l.remove(1)\n"
    )
    assert check_script(ok) == []


def test_staged_input_is_a_private_copy(tmp_path):
    # A script that chmods and appends to its input must not touch the original:
    # the full run stages the content-addressed retention copy of the evidence.
    inp = tmp_path / "in.log"
    inp.write_text("x\n")
    inp.chmod(0o644)
    before = inp.stat().st_mode
    script = (
        "import sys, pathlib\n"
        "p = pathlib.Path(sys.argv[sys.argv.index('-i') + 1])\n"
        "p.chmod(0o644)\n"
        "open(p, 'a').write('TAMPERED')\n"
    )
    r = run_converter(
        script,
        inp,
        output_path=tmp_path / "out" / "out.parquet",
        timeout_s=20,
        memory_mb=2048,
        output_mb=64,
    )
    assert r.exit_code == 0, r.stderr_tail
    assert inp.read_text() == "x\n"
    assert inp.stat().st_mode == before


def test_stderr_without_newlines_is_bounded(tmp_path):
    # RLIMIT_FSIZE does not cover pipes; a partial line must not grow without
    # bound in the API process. 8 MiB of no-newline stderr → a 4 KiB tail.
    script = "import sys\nsys.stderr.write('x' * (8 * 1024 * 1024))\nsys.stderr.flush()\n"
    r = _run(tmp_path, script)
    assert r.exit_code == 0
    assert len(r.stderr_tail) <= 4096 and r.stderr_tail.endswith("x")
