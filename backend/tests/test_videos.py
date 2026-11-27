"""Tests for the video endpoint (ROADMAP task 2.4, SPEC.md sections 6 and 10).

The order matters: names are rejected before anything touches the filesystem, a real file
is then streamed with the headers a player needs, and the last test renders an actual clip
and plays the bytes the endpoint returned. Requests go through the ASGI interface directly
rather than an HTTP client because a client normalizes ``..`` out of a URL before sending
it, which is exactly the behaviour these tests have to observe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import av
import pytest

from backend.app import cache, jobs, videos
from backend.app import main as api_main
from backend.app.render import OUTPUT_EXTENSION, QUALITY_PRESETS

KEY = hashlib.sha256(b"video-endpoint").hexdigest()
PINNED = {"app_version": "0.0.0", "manim_version": "0.22.1", "template_version": "1"}
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"body" * 50


def cache_file_name(quality: str = "preview") -> str:
    return cache.video_file_name(KEY, quality)


class Receive:
    """A stub ASGI ``receive`` that behaves like uvicorn's.

    uvicorn advertises ASGI spec version 2.3, which makes ``FileResponse`` poll ``receive``
    for a client disconnect while it streams (responses.py). A stub that answered instantly
    would look like a client that hung up and cancel the body, so this one reports the
    request once, then waits for the response to finish before disconnecting.
    """

    def __init__(self, body: bytes = b"") -> None:
        self._body = body
        self._reported = False
        self._finished = threading.Event()

    def response_finished(self) -> None:
        self._finished.set()

    async def __call__(self) -> dict[str, Any]:
        if not self._reported:
            self._reported = True
            return {"type": "http.request", "body": self._body, "more_body": False}
        deadline = time.monotonic() + 10.0
        while not self._finished.is_set():
            if time.monotonic() > deadline:
                break
            # Sleep, never block: the send side that sets the flag runs in this same loop.
            await asyncio.sleep(0.005)
        return {"type": "http.disconnect"}


def asgi_request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    query: bytes = b"",
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    """Call the app with one request and return (status, response headers, body)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1"), *headers],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    start: dict[str, Any] = {}
    body = bytearray()
    receive = Receive()

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            start["status"] = int(message["status"])
            start["headers"] = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                receive.response_finished()

    asyncio.run(app(scope, receive, send))
    return int(start.get("status", 0)), start.get("headers", {}), bytes(body)


def error_code(body: bytes) -> str:
    return str(json.loads(body)["error"]["code"])


@pytest.fixture
def video_name() -> str:
    return cache_file_name()


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """A render cache holding one real file under ``cache_file_name()``."""
    directory = tmp_path / "videos"
    directory.mkdir()
    (directory / cache_file_name()).write_bytes(VIDEO_BYTES)
    return directory


@pytest.fixture
def client(cache_dir: Path) -> Any:
    """The app wired to that cache, with a renderer no test in this file needs."""
    return api_main.create_app(jobs.JobQueue(cache_dir, key_versions=PINNED))


# ------------------------------------------------------------------- name validation


def test_a_valid_name_splits_into_the_key_and_the_quality() -> None:
    for quality in QUALITY_PRESETS:
        assert videos.split_name(cache.video_file_name(KEY, quality)) == (KEY, quality)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        ".",
        "../",
        KEY,
        f"{KEY}-preview",
        f"{KEY}-preview.{OUTPUT_EXTENSION}.txt",
        f"{KEY}-preview.{OUTPUT_EXTENSION}\n",
        f"{KEY}-preview.exe",
        f"{KEY}-preview.{OUTPUT_EXTENSION}\x00",
        f"{KEY.upper()}-preview.{OUTPUT_EXTENSION}",
        f"{KEY[:-1]}-preview.{OUTPUT_EXTENSION}",
        f"{KEY}0-preview.{OUTPUT_EXTENSION}",
        f"{KEY}-4k.{OUTPUT_EXTENSION}",
        f"{KEY}--preview.{OUTPUT_EXTENSION}",
        f"{KEY}-preview .{OUTPUT_EXTENSION}",
        f" {KEY}-preview.{OUTPUT_EXTENSION}",
        f"{KEY}/{OUTPUT_EXTENSION}",
        f"{KEY}\\preview.{OUTPUT_EXTENSION}",
        f"logs/{cache_file_name()}",
        f"..{OUTPUT_EXTENSION}",
        f"~{cache_file_name()}",
        f"%2e%2e%2f{cache_file_name()}",
        f"sub/{cache_file_name()}",
        "/etc/passwd",
        f"{KEY}-preview.mp4;",
        f"{KEY}-preview.mp4/",
        ("İ" * 64) + f"-preview.{OUTPUT_EXTENSION}",
    ],
)
def test_a_name_the_cache_could_not_produce_is_rejected(name: str) -> None:
    with pytest.raises(videos.VideoError) as excinfo:
        videos.split_name(name)
    assert excinfo.value.code == "video_name_invalid"


def test_the_error_never_repeats_the_name_that_was_asked_for() -> None:
    hostile = f"../../secret.{OUTPUT_EXTENSION}"
    with pytest.raises(videos.VideoError) as excinfo:
        videos.split_name(hostile)
    assert hostile not in excinfo.value.message
    assert "\n" not in excinfo.value.message


def test_resolve_only_points_inside_the_cache(tmp_path: Path) -> None:
    resolved = videos.resolve(tmp_path / "videos", cache_file_name())
    assert resolved.parent == tmp_path / "videos"
    assert resolved.name == cache_file_name()


def test_the_media_type_matches_what_the_renderer_writes() -> None:
    from_extension = f"video/{OUTPUT_EXTENSION}"
    assert from_extension == videos.VIDEO_MEDIA_TYPE


# ------------------------------------------------------------------------ streaming


def test_a_cached_video_streams_with_player_headers(client: Any, video_name: str) -> None:
    status, headers, body = asgi_request(client, f"/api/videos/{video_name}")
    assert status == 200
    assert body == VIDEO_BYTES
    assert headers["content-type"] == videos.VIDEO_MEDIA_TYPE
    assert headers["content-length"] == str(len(VIDEO_BYTES))
    assert headers["accept-ranges"] == "bytes"
    assert headers["content-disposition"] == f'inline; filename="{video_name}"'
    assert headers.get("etag") and headers.get("last-modified")


def test_download_asks_for_an_attachment(client: Any, video_name: str) -> None:
    status, headers, body = asgi_request(client, f"/api/videos/{video_name}", query=b"download=1")
    assert status == 200
    assert headers["content-disposition"] == f'attachment; filename="{video_name}"'
    assert body == VIDEO_BYTES


@pytest.mark.parametrize("query", [b"", b"download=", b"download=0", b"download=true", b"dl=1"])
def test_only_the_documented_query_value_downloads(
    client: Any, video_name: str, query: bytes
) -> None:
    _, headers, _ = asgi_request(client, f"/api/videos/{video_name}", query=query)
    assert headers["content-disposition"].startswith("inline")


def test_head_reports_the_size_without_the_body(client: Any, video_name: str) -> None:
    status, headers, body = asgi_request(client, f"/api/videos/{video_name}", method="HEAD")
    assert status == 200
    assert headers["content-length"] == str(len(VIDEO_BYTES))
    assert body == b""


def test_a_range_request_is_answered_so_the_player_can_seek(client: Any, video_name: str) -> None:
    status, headers, body = asgi_request(
        client, f"/api/videos/{video_name}", headers=((b"range", b"bytes=0-99"),)
    )
    assert status == 206
    assert body == VIDEO_BYTES[:100]
    assert headers["content-range"] == f"bytes 0-99/{len(VIDEO_BYTES)}"


def test_post_is_not_a_way_to_fetch_a_video(client: Any, video_name: str) -> None:
    status, _, _ = asgi_request(client, f"/api/videos/{video_name}", method="POST")
    assert status == 405


# --------------------------------------------------------------------------- misses


def test_a_well_formed_name_with_no_file_is_404(client: Any) -> None:
    missing = cache.video_file_name(hashlib.sha256(b"never rendered").hexdigest(), "preview")
    status, _, body = asgi_request(client, f"/api/videos/{missing}")
    assert status == 404
    assert error_code(body) == "video_not_found"


def test_an_empty_file_is_reported_missing_rather_than_streamed(
    client: Any, cache_dir: Path, video_name: str
) -> None:
    (cache_dir / video_name).write_bytes(b"")
    status, _, body = asgi_request(client, f"/api/videos/{video_name}")
    assert status == 404
    assert error_code(body) == "video_not_found"


def test_a_directory_named_like_a_video_is_not_served(
    client: Any, cache_dir: Path, video_name: str
) -> None:
    (cache_dir / video_name).unlink()
    (cache_dir / video_name).mkdir()
    status, _, body = asgi_request(client, f"/api/videos/{video_name}")
    assert status == 404
    assert error_code(body) == "video_not_found"


@pytest.mark.parametrize("name", ["app.log", "not-a-video", f"{KEY}-preview.txt", KEY])
def test_an_unusable_name_is_400_with_a_stable_code(client: Any, name: str) -> None:
    status, _, body = asgi_request(client, f"/api/videos/{name}")
    assert status == 400
    assert error_code(body) == "video_name_invalid"


# ---------------------------------------------------------------------- traversal


@pytest.mark.parametrize(
    "path",
    [
        "/api/videos/..",
        "/api/videos/../",
        f"/api/videos/../{cache_file_name()}",
        f"/api/videos/%2e%2e%2f%2e%2e%2fsecret.{OUTPUT_EXTENSION}",
        f"/api/videos/....//....//secret.{OUTPUT_EXTENSION}",
        f"/api/videos/%2e%2e/{cache_file_name()}",
        f"/api/videos/sub/{cache_file_name()}",
        "/api/videos/C:/Windows/win.ini",
        "/api/videos//etc/hosts",
        f"/api/videos/{cache_file_name()}/..",
        f"/api/videos/{cache_file_name()}%2f..",
    ],
)
def test_a_name_with_a_slash_is_refused_without_being_resolved(client: Any, path: str) -> None:
    """A slash cannot survive the single-segment route, so nothing below it is ever opened."""
    status, _, body = asgi_request(client, path)
    assert status == 400, path
    assert error_code(body) == "video_name_invalid"
    assert VIDEO_BYTES not in body


def test_a_file_outside_the_cache_is_unreachable_even_by_name(tmp_path: Path) -> None:
    """The decoy is a real, validly named video file one directory above the cache."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / cache_file_name()).write_bytes(VIDEO_BYTES)
    app = api_main.create_app(jobs.JobQueue(tmp_path / "videos", key_versions=PINNED))
    status, _, body = asgi_request(app, f"/api/videos/../outside/{cache_file_name()}")
    assert status == 400
    assert body != VIDEO_BYTES


def test_the_cache_may_live_where_no_video_exists_yet(tmp_path: Path) -> None:
    app = api_main.create_app(jobs.JobQueue(tmp_path / "absent", key_versions=PINNED))
    status, _, body = asgi_request(app, f"/api/videos/{cache_file_name()}")
    assert status == 404
    assert error_code(body) == "video_not_found"
    assert not (tmp_path / "absent").exists()


# ------------------------------------------------------------------- end to end


def test_the_url_a_finished_job_advertises_serves_those_bytes(tmp_path: Path) -> None:
    """The whole contract: render, poll, then fetch the URL the job reported."""

    def stub(params: dict[str, Any], output_dir: Path, stem: str) -> Path:
        produced = output_dir / f"{stem}.{OUTPUT_EXTENSION}"
        produced.parent.mkdir(parents=True, exist_ok=True)
        produced.write_bytes(VIDEO_BYTES)
        return produced

    queue = jobs.JobQueue(tmp_path / "videos", renderer=stub, key_versions=PINNED)
    app = api_main.create_app(queue)
    status, response_body = post_render(app, {"text": "Serve me"})
    assert status == 202
    payload = wait_for_status(queue, json.loads(response_body)["job_id"])
    assert payload["status"] == jobs.DONE

    get_status, headers, body = asgi_request(app, payload["video_url"])
    assert get_status == 200
    assert headers["content-type"] == videos.VIDEO_MEDIA_TYPE
    assert body == VIDEO_BYTES


def post_render(app: Any, body: dict[str, Any]) -> tuple[int, bytes]:
    payload = json.dumps(body).encode("utf-8")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/render",
        "raw_path": b"/api/render",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    status: dict[str, int] = {}
    out = bytearray()
    receive = Receive(payload)

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            out.extend(message.get("body", b""))
            if not message.get("more_body"):
                receive.response_finished()

    asyncio.run(app(scope, receive, send))
    return status.get("code", 0), bytes(out)


def wait_for_status(
    queue: jobs.JobQueue, job_id: str, *, timeout: float = 5.0
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = queue.payload(job_id)
        if payload and payload["status"] in (jobs.DONE, jobs.FAILED):
            return payload
        time.sleep(0.02)
    return payload


def test_a_really_rendered_clip_plays_after_it_is_served(tmp_path: Path) -> None:
    """ROADMAP 2.4 acceptance: a valid file streams and plays."""
    queue = jobs.JobQueue(tmp_path / "videos", key_versions=PINNED)
    app = api_main.create_app(queue)
    status, response_body = post_render(
        app, {"text": "Real", "animation_duration": 0.5, "hold_duration": 0.5}
    )
    assert status == 202
    payload = wait_for_status(queue, json.loads(response_body)["job_id"], timeout=180.0)
    assert payload is not None and payload["status"] == jobs.DONE, payload

    get_status, headers, body = asgi_request(app, payload["video_url"])
    assert get_status == 200
    assert headers["content-disposition"].startswith("inline")
    assert len(body) == int(headers["content-length"]) > 1000

    played = tmp_path / "played.mp4"
    played.write_bytes(body)
    with av.open(str(played)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == (854, 480)
        assert float(stream.average_rate) == pytest.approx(15.0)
        assert sum(1 for _ in container.decode(stream)) > 0
