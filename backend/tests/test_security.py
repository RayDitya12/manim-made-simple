"""Tests for host and origin hardening (ROADMAP task 2.5, SPEC.md section 10).

Three layers, in this order: the ``Host`` header parser on its own, the guard as the app sees
it (a refused request must not reach a route), and the CORS policy's answers. Requests are
driven through the ASGI interface because the whole point is to control one header that an
HTTP client sets for you, and a real render is checked at the end so the pure-ASGI guard is
proven against the streaming video response too.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from pathlib import Path
from typing import Any

import pytest

from backend.app import jobs, security
from backend.app import main as api_main

GOOD_HOSTS = ("127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:8123", "[::1]:8000")
BAD_HOSTS = (
    "",
    "evil.example.com",
    "127.0.0.1.evil.com",
    "localhost.evil.com",
    "10.0.0.5:8000",
    "0.0.0.0:8000",
    "127.0.0.1:8000.evil.com",
    "http://127.0.0.1",
    "@127.0.0.1",
    "127.0.0.1:x80",
    "127.0.0.1:8000 evil.com",
    "[::1",
    "[::1]8000",
    "127.0.0.1:8000]",
)


def asgi_call(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    host: str | None = "127.0.0.1",
    origin: str | None = None,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    """Send one request with a chosen ``Host`` and ``Origin`` and read the response back."""
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if host is not None:
        headers.append((b"host", host.encode("latin-1")))
    if origin is not None:
        headers.append((b"origin", origin.encode("latin-1")))
    headers.extend(extra_headers)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    start: dict[str, Any] = {}
    out = bytearray()
    state = {"reported": False, "finished": False}

    async def receive() -> dict[str, Any]:
        if not state["reported"]:
            state["reported"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        while not state["finished"]:
            await asyncio.sleep(0.005)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            start["status"] = int(message["status"])
            start["headers"] = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            out.extend(message.get("body", b""))
            if not message.get("more_body"):
                state["finished"] = True

    asyncio.run(app(scope, receive, send))
    return int(start.get("status", 0)), start.get("headers", {}), bytes(out)


def error_code(body: bytes) -> str:
    return str(json.loads(body)["error"]["code"])


@pytest.fixture
def app(tmp_path: Path) -> Any:
    """The app on a cache directory with one video in it, and a renderer no test needs."""
    video_dir = tmp_path / "videos"
    video_dir.mkdir()
    (video_dir / "ab12-preview.mp4").write_bytes(b"not a real video")
    return api_main.create_app(jobs.JobQueue(video_dir, key_versions={}))


# --------------------------------------------------------------------- Host header


@pytest.mark.parametrize(
    "value, expected",
    [
        ("127.0.0.1:8000", ("127.0.0.1", "8000")),
        ("localhost", ("localhost", "")),
        ("[::1]:8000", ("::1", "8000")),
        ("LocalHost:80", ("localhost", "80")),
        ("127.0.0.1:8000 ", ("127.0.0.1", "8000")),
        ("[::1", ("", "")),
        ("[::1]8000", ("", "")),
    ],
)
def test_the_host_header_splits_into_name_and_port(value: str, expected: tuple[str, str]) -> None:
    assert security.host_parts(value) == expected


@pytest.mark.parametrize("value", GOOD_HOSTS)
def test_a_loopback_host_is_allowed(value: str) -> None:
    assert security.is_allowed_host(value)


@pytest.mark.parametrize("value", BAD_HOSTS)
def test_any_other_host_is_refused(value: str) -> None:
    assert not security.is_allowed_host(value)


def test_a_host_header_that_is_absent_is_refused() -> None:
    assert not security.is_allowed_host(None)


# ------------------------------------------------------------------ the app's answer


def test_the_ui_hosts_still_get_an_answer(app: Any) -> None:
    for host in GOOD_HOSTS:
        status, _, body = asgi_call(app, "/api/health", host=host)
        assert status == 200, host
        assert json.loads(body)["status"] == "ok"


@pytest.mark.parametrize("host", BAD_HOSTS)
def test_a_foreign_host_never_reaches_a_route(app: Any, host: str) -> None:
    status, _, body = asgi_call(app, "/api/health", host=host)
    assert status == 400, host
    assert error_code(body) == "host_not_allowed"


def test_a_rebinding_attempt_cannot_queue_a_render(app: Any) -> None:
    """The hostile case: an attacker's page posts a render through the visitor's own loopback."""
    payload = json.dumps({"text": "Render for me"}).encode("utf-8")
    status, _, body = asgi_call(
        app,
        "/api/render",
        method="POST",
        host="evil.example.com",
        origin="http://evil.example.com",
        body=payload,
    )
    assert status == 400
    assert error_code(body) == "host_not_allowed"
    assert b"job_id" not in body


def test_a_rebinding_attempt_cannot_read_a_video(app: Any) -> None:
    status, _, body = asgi_call(app, "/api/videos/ab12-preview.mp4", host="attacker.invalid")
    assert status == 400
    assert body != b"not a real video"


def test_a_missing_host_header_is_refused(app: Any) -> None:
    status, _, body = asgi_call(app, "/api/health", host=None)
    assert status == 400
    assert error_code(body) == "host_not_allowed"


def test_two_host_headers_are_refused_rather_than_guessed(app: Any) -> None:
    status, _, body = asgi_call(
        app,
        "/api/health",
        host="127.0.0.1",
        extra_headers=((b"host", b"evil.example.com"),),
    )
    assert status == 400
    assert error_code(body) == "host_not_allowed"


def test_the_guard_answers_localhost_with_a_real_render(tmp_path: Path) -> None:
    """The hardening must not break streaming: a real clip still arrives byte for byte."""
    queue = jobs.JobQueue(tmp_path / "videos", key_versions={})
    application = api_main.create_app(queue)
    payload = json.dumps({"text": "Guarded", "animation_duration": 0.5, "hold_duration": 0.5})
    status, _, body = asgi_call(
        application,
        "/api/render",
        method="POST",
        host="localhost:8000",
        body=payload.encode("utf-8"),
    )
    assert status == 202
    job_id = str(json.loads(body)["job_id"])

    deadline = time.monotonic() + 180.0
    report: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        report = queue.payload(job_id)
        assert report is not None
        if report["status"] in (jobs.DONE, jobs.FAILED):
            break
        time.sleep(0.2)
    assert report is not None and report["status"] == jobs.DONE, report

    get_status, headers, video = asgi_call(application, report["video_url"], host="127.0.0.1:8000")
    assert get_status == 200
    assert headers["content-type"] == "video/mp4"
    assert len(video) == int(headers["content-length"]) > 1000


# ---------------------------------------------------------------------------- CORS


def test_a_localhost_preflight_is_approved(app: Any) -> None:
    status, headers, _ = asgi_call(
        app,
        "/api/render",
        method="OPTIONS",
        origin="http://127.0.0.1:54321",
        extra_headers=(
            (b"access-control-request-method", b"POST"),
            (b"access-control-request-headers", b"content-type"),
        ),
    )
    assert status == 200
    assert headers["access-control-allow-origin"] == "http://127.0.0.1:54321"
    assert "POST" in headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in headers


def test_a_foreign_preflight_is_refused(app: Any) -> None:
    status, headers, _ = asgi_call(
        app,
        "/api/render",
        method="OPTIONS",
        origin="http://evil.example.com",
        extra_headers=((b"access-control-request-method", b"POST"),),
    )
    assert status == 400
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.com",
        "http://127.0.0.1.evil.com",
        "http://localhost:8000.evil.com",
        "https://localhost",
        "http://127.0.0.1:999999",
        "null",
        "http://evil.test@127.0.0.1:8000",
    ],
)
def test_only_a_loopback_origin_gets_headers(app: Any, origin: str) -> None:
    status, headers, _ = asgi_call(app, "/api/health", origin=origin)
    assert status == 200
    assert "access-control-allow-origin" not in headers


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://localhost:3000"])
def test_a_loopback_origin_is_named_and_vary_is_declared(app: Any, origin: str) -> None:
    status, headers, _ = asgi_call(app, "/api/health", origin=origin)
    assert status == 200
    assert headers["access-control-allow-origin"] == origin
    assert headers["vary"] == "Origin"


def test_a_bad_host_wins_over_a_friendly_origin(app: Any) -> None:
    """The guard is the outer layer, so a rebinding attempt learns nothing about the policy."""
    status, headers, body = asgi_call(
        app, "/api/health", host="rebind.attacker.test", origin="http://127.0.0.1:8000"
    )
    assert status == 400
    assert error_code(body) == "host_not_allowed"
    assert "access-control-allow-origin" not in headers


# ------------------------------------------------------------------------ the bind


def test_the_documented_bind_address_is_loopback() -> None:
    assert ipaddress.ip_address(security.LOOPBACK_HOST).is_loopback


def test_the_dev_runner_binds_loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        seen.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert api_main.main() == 0
    assert seen["host"] == security.LOOPBACK_HOST
    assert seen["host"] == "127.0.0.1"
