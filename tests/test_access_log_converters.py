"""Access-log parsing in the vendored nginx2timesketch and apache2timesketch (#437).

Upstream (overcuriousity/2timesketch) carries no tests; the vendored
single-file scripts are what Vestigo ships, so they are exercised here, loaded
from their asset paths like the native converters.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_ASSETS = Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters"

_ISSUE_437_LINE = (
    "www.example.com 82.158.193.164 - [07/Oct/2026:10:14:42 +0200] "
    '"POST /?rest_route=/batch/v1 HTTP/1.1" 403 239 '
    '"http://www.example.com/?rest_route=/batch/v1" "Mozilla/5.0 (Macintosh)" '
    '"https -" "ssl-proto TLSv1.3" "cipher TLS_AES_256_GCM_SHA384" '
    '"tls-sni www.example.com" "-" 1099 4975 1'
)
_COMBINED = '[07/Oct/2026:10:14:42 +0200] "GET / HTTP/1.1" 200 1 "-" "ua"'
_CLF = '[07/Oct/2026:10:14:42 +0200] "GET / HTTP/1.1" 200 1'


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ASSETS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def nginx():
    return _load("nginx2timesketch")


@pytest.fixture(scope="module")
def apache():
    return _load("apache2timesketch")


def _parse(module, line: str) -> dict | None:
    if hasattr(module, "_parse_apache_access_line"):
        return module._parse_apache_access_line(line, "src")
    return module._parse_access_line(line, "access", "src")


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_issue_line_is_read_unshifted(request, name):
    row = _parse(request.getfixturevalue(name), _ISSUE_437_LINE)
    assert row["src_ip"] == "82.158.193.164"
    assert row["client_host"] == "82.158.193.164"
    assert row["vhost"] == "www.example.com"
    assert row["remote_ident"] is None
    assert row["additional_field"] == "https -"
    assert row["trailing_fields"].startswith('"ssl-proto TLSv1.3"')
    assert row["trailing_fields"].endswith("1099 4975 1")


_LAYOUTS = [
    ("203.0.113.7 - alice", "203.0.113.7", "203.0.113.7", None, "alice"),
    (
        "www.example.com:443 198.51.100.9 - -",
        "198.51.100.9",
        "198.51.100.9",
        "www.example.com:443",
        None,
    ),
    ("www.example.com 198.51.100.9 - -", "198.51.100.9", "198.51.100.9", "www.example.com", None),
    ("192.0.2.10 82.158.193.164 -", "82.158.193.164", "82.158.193.164", "192.0.2.10", None),
    ("host.example.net - -", "", "host.example.net", None, None),
    (
        "www.example.com:443 host.example.net - -",
        "",
        "host.example.net",
        "www.example.com:443",
        None,
    ),
    ("10.0.0.1, 192.0.2.4 - -", "10.0.0.1", "10.0.0.1, 192.0.2.4", None, None),
    (
        "www.example.com 10.0.0.1, 192.0.2.4 - -",
        "10.0.0.1",
        "10.0.0.1, 192.0.2.4",
        "www.example.com",
        None,
    ),
]


@pytest.mark.parametrize("name", ["nginx", "apache"])
@pytest.mark.parametrize(("prefix", "src_ip", "client_host", "vhost", "remote_user"), _LAYOUTS)
def test_combined_layouts(request, name, prefix, src_ip, client_host, vhost, remote_user):
    row = _parse(request.getfixturevalue(name), f"{prefix} {_COMBINED}")
    assert (row["src_ip"], row["client_host"], row["vhost"], row["remote_user"]) == (
        src_ip,
        client_host,
        vhost,
        remote_user,
    )
    assert row["trailing_fields"] is None


@pytest.mark.parametrize(("prefix", "src_ip", "client_host", "vhost", "remote_user"), _LAYOUTS)
def test_apache_clf_layouts(apache, prefix, src_ip, client_host, vhost, remote_user):
    row = _parse(apache, f"{prefix} {_CLF}")
    assert (row["src_ip"], row["client_host"], row["vhost"], row["remote_user"]) == (
        src_ip,
        client_host,
        vhost,
        remote_user,
    )


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_chain_without_ident_slot_is_not_misattributed(request, name):
    # Ambiguous: the chain's first element ends in a comma and is never a vhost,
    # so the proxy is never read as the client. Counted as unparseable.
    assert _parse(request.getfixturevalue(name), f"192.0.2.1, 198.51.100.2 - {_COMBINED}") is None


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_directory_mode_finds_per_vhost_layouts(request, name, tmp_path):
    module = request.getfixturevalue(name)
    (tmp_path / "www.example.com").mkdir()
    (tmp_path / "www.example.com" / "access.log").write_text(f"203.0.113.7 - - {_COMBINED}\n")
    (tmp_path / "shop.example.com.access.log").write_text(f"203.0.113.8 - - {_COMBINED}\n")
    found = module._find_log_files(str(tmp_path))
    assert sorted(p.name for p in found["access"]) == ["access.log", "shop.example.com.access.log"]


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_absolute_glob_is_expanded(request, name, tmp_path):
    module = request.getfixturevalue(name)
    (tmp_path / "access.log").write_text(f"203.0.113.7 - - {_COMBINED}\n")
    # Path(".").glob() raised NotImplementedError on any absolute pattern.
    found = module._find_log_files(str(tmp_path / "acc*.log"))
    assert [p.name for p in found["access"]] == ["access.log"]


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_pipe_input_is_refused_cleanly(request, name, tmp_path):
    module = request.getfixturevalue(name)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(module.ConverterError, match="neither a regular file nor a directory"):
        module._find_log_files(str(fifo))


@pytest.mark.parametrize("name", ["nginx", "apache"])
def test_audit_report_counts_vhost_trailing_and_unparseable(request, name, tmp_path):
    import json

    module = request.getfixturevalue(name)
    log = tmp_path / "access.log"
    log.write_text(f"{_ISSUE_437_LINE}\n203.0.113.7 - - {_COMBINED}\nnot a log line\n")
    convert = module.convert_nginx if name == "nginx" else module.convert_apache
    report = tmp_path / "report.json"
    convert(
        str(log),
        str(tmp_path / "out.jsonl"),
        "jsonl",
        verbose=False,
        report_path=str(report),
    )
    stats = json.loads(report.read_text())["statistics"]
    assert stats["rows_unparseable"] == 1
    assert stats["rows_vhost_prefixed"] == 1
    assert stats["rows_with_trailing_fields"] == 1
