"""Synthetic tool payloads with the character shape that broke the estimator.

``vestigo.agent.window.estimate_tokens`` is ``chars / N``. Whether that
heuristic holds depends entirely on *what kind* of characters a payload
carries, and every pre-existing window test built its payloads from ASCII
filler (``"x" * 4000``), where ``chars/4`` is roughly right. Real tool results
are not filler: escaped JSON quotes, base64 ``state=`` parameters, dotted-quad
IPs and UUID event ids all tokenize near one token per two characters.

Measured on the 2026-07-23 overflow (`docs/PROGRESS.md`): a 178896-char request
was counted by the provider as 75967 tokens — **2.35 chars/token**, not 4.

The values here are synthesized rather than copied from the real capture on
purpose: that conversation is a live case containing a school's domain, pupil
account names and client IP addresses. The regression this fixture guards is
about payload *shape* (character-class mix and size), which is reproduced
faithfully; none of the personal data is needed for it and none is committed.
"""

from __future__ import annotations

import json
from typing import Any

#: Provider ground truth from the 2026-07-23 LiteLLM overflow body:
#: "request (75967 tokens) exceeds the available context size (65536 tokens)"
#: paired with that request's Content-Length. The one real measurement of this
#: deployment's chars-per-token, and the reference the estimator is held to.
OVERFLOW_REQUEST_CHARS = 178_896
OVERFLOW_REQUEST_TOKENS = 75_967
OVERFLOW_CHARS_PER_TOKEN = OVERFLOW_REQUEST_CHARS / OVERFLOW_REQUEST_TOKENS  # ~2.355

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# A base64url JWT-ish blob, the shape of a real `?state=` redirect parameter.
# Single-token-per-2-3-chars territory, and the densest thing in the capture.
_STATE = (
    "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6IjEifQ"
    "eyJyZWRpcmVjdF91cmkiOiJodHRwczpcL1wvZXhhbXBsZS5pbnZhbGlkXC9hcHBcL21haWxc"
    "L3VzZXItMDAxQGV4YW1wbGUuaW52YWxpZFwvSU5CT1giLCJub25jZSI6IjVjM2E2ZWIzLTc4"
    "NTktNDQ2My04MjAyLWZkMzcwZTQxNjk1YSIsImFkbWluIjpmYWxzZX0"
)


def synthetic_event(index: int) -> list[Any]:
    """One event row in the columnar shape ``search_events`` returns."""
    octet = index % 256
    ip = f"203.0.113.{octet}"  # TEST-NET-3, reserved for documentation
    event_id = f"{index:08x}-ff70-53fa-bfff-e1073cf7f{index % 1000:03d}"
    uri = f"/app/authentication/redirect?state={_STATE}" if index % 4 == 0 else "/app/"
    return [
        event_id,
        f"2026-01-27T0{index % 8}:00:00+00:00",
        "Case_f24c250d_f049cc8e07937c10a6cdcf0d944254179a893c957_639dd462",
        "nginx:access",
        f'{ip} -  [27/Jan/2026:09:00:00 +0100] "GET {uri} HTTP/2.0" 502 0 "-" "{_UA}"',
        {
            "log_type": "access",
            "src_ip": ip,
            "http_method": "GET",
            "http_uri": uri,
            "http_protocol": "HTTP/2.0",
            "http_request_full": f"GET {uri} HTTP/2.0",
            "status_code": "502",
            "response_size": "0",
            "user_agent": _UA,
        },
    ]


def search_events_result(rows: int = 30) -> dict[str, Any]:
    """A ``search_events`` payload of the shape and density that overflowed.

    The real capture's largest single result was 21454 chars for 30 rows; this
    lands in the same range without carrying any of its data.
    """
    return {
        "total": 192_254_763,
        "returned": rows,
        "events": {
            "columns": [
                "event_id",
                "timestamp",
                "source_id",
                "artifact",
                "message",
                "attributes",
            ],
            "rows": [synthetic_event(i) for i in range(rows)],
        },
        "fidelity": "full",
    }


def payload_of_size(target_chars: int) -> dict[str, Any]:
    """A realistic payload whose JSON dump is at least ``target_chars`` long."""
    rows = 30
    while len(json.dumps(search_events_result(rows), default=str)) < target_chars:
        rows *= 2
    return search_events_result(rows)
