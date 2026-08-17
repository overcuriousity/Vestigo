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
