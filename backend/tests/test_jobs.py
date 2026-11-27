"""Tests for the job queue and the two job endpoints (ROADMAP task 2.3).

Most of the file drives a :class:`~backend.app.jobs.JobQueue` whose renderer is a stub, so
the queue's own promises -- one render at a time, FIFO, cache first, a stable error when a
render fails -- are checked in milliseconds instead of waiting on Manim. The last section
drops the stub and renders one real preview clip end to end, because the point of the queue
is to produce a file the app can play.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import av
import pytest

from backend.app import cache, data_dir, jobs
from backend.app import main as api_main
from backend.app.models import RenderParams, parse_params
from backend.app.render import RenderError

#: Pinned so a queue test does not depend on which Manim happens to be installed.
PINNED = {"app_version": "0.0.0", "manim_version": "0.22.1", "template_version": "1"}
TEXT = {"text": "Queue me"}


# --------------------------------------------------------------------------- helpers


class StubRenderer:
    """A stand-in for ``render_video``: records its calls, can be held and made to fail."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.gate: threading.Event | None = None
        self.failure: BaseException | None = None
        self._lock = threading.Lock()

    def __call__(self, params: dict[str, Any], output_dir: Path, file_stem: str) -> Path:
        with self._lock:
            self.calls.append((params["text"], file_stem))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                self.gate.wait(5.0)
            if self.failure is not None:
                raise self.failure
            # render_video creates the output directory, so the stub does too.
            path = Path(output_dir) / f"{file_stem}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stub video bytes")
            return path
        finally:
            with self._lock:
                self.active -= 1

    def texts(self) -> list[str]:
        return [call[0] for call in self.calls]

    def wait_until_busy(self, timeout: float = 5.0) -> None:
        """Block until the worker is inside a render, so ordering claims mean something."""
        deadline = time.monotonic() + timeout
        while self.active == 0:
            if time.monotonic() > deadline:
                raise AssertionError("the worker never started the queued render")
            time.sleep(0.005)


def make_queue(tmp_path: Path, renderer: StubRenderer | None = None) -> jobs.JobQueue:
    render = renderer if renderer is not None else StubRenderer()
    return jobs.JobQueue(tmp_path / "videos", renderer=render, key_versions=PINNED)


def wait_for(queue: jobs.JobQueue, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    """Wait until the job is ``done`` or ``failed`` and return its payload."""
    payload: dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = queue.payload(job_id) or {}
        if payload.get("status") in {jobs.DONE, jobs.FAILED}:
            return payload
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} never settled (last: {payload})")


def worker_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "mms-jobs"]


def wait_for_worker_exit(timeout: float = 5.0) -> None:
    """Block until the queue's worker thread is gone, so restarts are not racing it."""
    deadline = time.monotonic() + timeout
    while worker_threads() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert worker_threads() == [], "a stopped worker thread is still running"


def asgi_request(
    app: Any,
    method: str,
    path: str,
    body: Any = None,
    raw_body: bytes | None = None,
) -> tuple[int, Any]:
    """Send one request through the ASGI interface and return (status, decoded body)."""
    if raw_body is not None:
        payload = raw_body
    elif body is None:
        payload = b""
    else:
        payload = json.dumps(body).encode("utf-8")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
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
    received = bytearray()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            received.extend(message.get("body", b""))

    async def call() -> None:
        await app(scope, receive, send)

    asyncio.run(call())
    return status.get("code", 0), json.loads(bytes(received) or b"null")


def submit_and_wait(app: Any, body: Any, timeout: float = 5.0) -> tuple[int, dict, dict]:
    """POST a render, poll it to a terminal state, and hand back both responses."""
    post_status, submit = asgi_request(app, "POST", "/api/render", body)
    assert "job_id" in submit, submit
    poll: dict[str, Any] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, poll = asgi_request(app, "GET", f"/api/jobs/{submit['job_id']}")
        if poll.get("status") in {jobs.DONE, jobs.FAILED}:
            return post_status, submit, poll
        time.sleep(0.02)
    raise AssertionError(f"job {submit['job_id']} never settled (last: {poll})")


@pytest.fixture
def renderer() -> StubRenderer:
    return StubRenderer()


@pytest.fixture
def queue(tmp_path: Path, renderer: StubRenderer) -> Any:
    """A running queue backed by the stub renderer, stopped at teardown."""
    job_queue = make_queue(tmp_path, renderer)
    job_queue.start()
    try:
        yield job_queue
    finally:
        job_queue.stop()


@pytest.fixture
def app(tmp_path: Path, renderer: StubRenderer) -> Any:
    """A live app whose queue is stopped at teardown, so no worker outlives the test."""
    job_queue = make_queue(tmp_path, renderer)
    application = api_main.create_app(job_queue=job_queue)
    try:
        yield application
    finally:
        job_queue.stop()


# --------------------------------------------------------------- the queue's promises


def test_a_job_reaches_done_and_names_its_video(queue: jobs.JobQueue, tmp_path: Path) -> None:
    job, was_cached = queue.submit(parse_params(TEXT))
    assert was_cached is False
    payload = wait_for(queue, job.id)
    assert payload["job_id"] == job.id
    assert payload["status"] == jobs.DONE
    assert payload["error"] is None
    assert payload["video_url"] == f"/api/videos/{job.file_name}"
    assert (tmp_path / "videos" / job.file_name).read_bytes() == b"stub video bytes"


def test_payload_is_exactly_the_documented_fields(queue: jobs.JobQueue) -> None:
    job, _ = queue.submit(parse_params(TEXT))
    wait_for(queue, job.id)
    assert set(queue.payload(job.id) or {}) == {"job_id", "status", "video_url", "error"}


def test_the_four_statuses_are_the_ones_the_spec_names() -> None:
    """The UI branches on these strings, so they are part of the contract (SPEC.md section 6)."""
    assert jobs.STATUSES == ("queued", "running", "done", "failed")


def test_a_job_still_queued_when_the_worker_stops_runs_after_a_restart(tmp_path: Path) -> None:
    """stop() ends the worker without losing the work it had not reached yet."""
    renderer = StubRenderer()
    renderer.gate = threading.Event()
    queue = make_queue(tmp_path, renderer)
    queue.start()
    assert renderer.gate is not None
    try:
        busy, _ = queue.submit(parse_params({"text": "Busy"}))
        renderer.wait_until_busy()
        waiting, _ = queue.submit(parse_params({"text": "Waiting"}))
        assert queue.payload(waiting.id)["status"] == jobs.QUEUED
        # The worker is inside the gated render, so it is stopped with real work pending.
        queue.stop(timeout=0.05)
        renderer.gate.set()
        assert wait_for(queue, busy.id, timeout=10.0)["status"] == jobs.DONE
        assert queue.payload(waiting.id)["status"] == jobs.QUEUED, (
            "a stopped worker must not start new renders"
        )
        wait_for_worker_exit()

        queue.start()
        assert wait_for(queue, waiting.id, timeout=10.0)["status"] == jobs.DONE
    finally:
        renderer.gate.set()
        queue.stop(timeout=0.5)


def test_a_job_reports_running_before_it_reports_done(tmp_path: Path) -> None:
    """The middle state exists, so the UI can show progress instead of a fake percentage."""
    renderer = StubRenderer()
    renderer.gate = threading.Event()
    queue = make_queue(tmp_path, renderer)
    queue.start()
    try:
        job, _ = queue.submit(parse_params(TEXT))
        renderer.wait_until_busy()
        running = queue.payload(job.id)
        assert running["status"] == jobs.RUNNING
        assert running["video_url"] is None
        assert renderer.gate is not None
        renderer.gate.set()
        assert wait_for(queue, job.id)["status"] == jobs.DONE
    finally:
        assert renderer.gate is not None
        renderer.gate.set()
        queue.stop()


def test_job_ids_are_hex_and_only_hex_ids_are_looked_up(queue: jobs.JobQueue) -> None:
    job, _ = queue.submit(parse_params(TEXT))
    wait_for(queue, job.id)
    assert jobs.JOB_ID_PATTERN.fullmatch(job.id)
    assert queue.payload(job.id) is not None
    for hostile in ["", "../etc/passwd", "0" * 31, "Z" * 32, f"{job.id} OR 1=1"]:
        assert queue.payload(hostile) is None


def test_jobs_run_one_at_a_time_in_the_order_they_arrived(
    tmp_path: Path, renderer: StubRenderer
) -> None:
    """SPEC.md section 6: a single in-process worker, one render at a time, FIFO."""
    renderer.gate = threading.Event()
    held = make_queue(tmp_path, renderer)
    held.start()
    try:
        first, _ = held.submit(parse_params({"text": "First"}))
        second, _ = held.submit(parse_params({"text": "Second"}))
        renderer.wait_until_busy()
        assert renderer.texts() == ["First"], "the second job must wait for the first"
        assert held.payload(second.id)["status"] == jobs.QUEUED
        assert renderer.max_active == 1

        assert renderer.gate is not None
        renderer.gate.set()
        assert wait_for(held, first.id)["status"] == jobs.DONE
        assert wait_for(held, second.id)["status"] == jobs.DONE
        assert renderer.texts() == ["First", "Second"]
        assert renderer.max_active == 1, "two renders ran in parallel"
    finally:
        assert renderer.gate is not None
        renderer.gate.set()
        held.stop()


def test_a_finished_file_answers_the_next_request_as_cached(
    queue: jobs.JobQueue, renderer: StubRenderer
) -> None:
    """The ROADMAP 2.3 acceptance rule: the identical request is served from the cache."""
    first, _ = queue.submit(parse_params(TEXT))
    assert wait_for(queue, first.id)["video_url"] == f"/api/videos/{first.file_name}"
    renderer.calls.clear()

    started = time.monotonic()
    second, was_cached = queue.submit(parse_params(TEXT))
    assert was_cached is True, "the file exists, so the render must be reused"
    assert time.monotonic() - started < 1.0, "a cache hit must not wait for a render"
    assert queue.payload(second.id)["status"] == jobs.DONE
    assert queue.payload(second.id)["video_url"] == queue.payload(first.id)["video_url"]
    assert renderer.calls == []


def test_a_duplicate_queued_while_the_first_renders_does_not_render_twice(
    tmp_path: Path, renderer: StubRenderer
) -> None:
    renderer.gate = threading.Event()
    held = make_queue(tmp_path, renderer)
    held.start()
    try:
        first, _ = held.submit(parse_params(TEXT))
        renderer.wait_until_busy()
        second, was_cached = held.submit(parse_params(TEXT))
        assert was_cached is False, "the file does not exist yet, so this cannot be a hit"
        assert renderer.gate is not None
        renderer.gate.set()
        assert wait_for(held, first.id)["status"] == jobs.DONE
        assert wait_for(held, second.id)["status"] == jobs.DONE
        assert len(renderer.calls) == 1, "the worker re-checks the cache before it renders"
        assert held.payload(second.id)["video_url"] == held.payload(first.id)["video_url"]
    finally:
        assert renderer.gate is not None
        renderer.gate.set()
        held.stop()


def test_hd_is_a_separate_job_from_preview(queue: jobs.JobQueue, renderer: StubRenderer) -> None:
    """One parameter set at two qualities is two files, so a preview never answers HD."""
    preview, _ = queue.submit(parse_params(TEXT))
    assert wait_for(queue, preview.id)["status"] == jobs.DONE
    hd, cached = queue.submit(parse_params({**TEXT, "quality": "hd"}))
    assert cached is False
    assert wait_for(queue, hd.id)["status"] == jobs.DONE
    assert hd.file_name.endswith("-hd.mp4")
    assert hd.file_name != preview.file_name
    assert renderer.texts() == [TEXT["text"], TEXT["text"]]


def test_a_render_error_becomes_the_stable_code_on_the_job(tmp_path: Path) -> None:
    renderer = StubRenderer()
    renderer.failure = RenderError(
        "render_timeout",
        "the render took longer than 60 seconds and was stopped.",
        {"limit": 60},
    )
    queue = make_queue(tmp_path, renderer)
    queue.start()
    try:
        job, _ = queue.submit(parse_params(TEXT))
        payload = wait_for(queue, job.id)
        assert payload["status"] == jobs.FAILED
        assert payload["video_url"] is None
        assert payload["error"] == {
            "code": "render_timeout",
            "message": "the render took longer than 60 seconds and was stopped.",
            "params": {"limit": 60},
        }
    finally:
        queue.stop()


def test_an_unexpected_failure_still_leaves_the_worker_alive(tmp_path: Path) -> None:
    """One crash must not strand every job queued behind it, nor leak its traceback."""
    renderer = StubRenderer()
    renderer.failure = ZeroDivisionError("a bug inside the render layer")
    queue = make_queue(tmp_path, renderer)
    queue.start()
    try:
        broken, _ = queue.submit(parse_params(TEXT))
        payload = wait_for(queue, broken.id)
        assert payload["error"] == {
            "code": "render_failed",
            "message": "the video could not be rendered.",
            "params": {},
        }
        assert "ZeroDivisionError" not in json.dumps(payload)

        renderer.failure = None
        working, _ = queue.submit(parse_params({"text": "After the crash"}))
        assert wait_for(queue, working.id)["status"] == jobs.DONE
    finally:
        queue.stop()


def test_an_empty_leftover_file_is_not_treated_as_done(
    tmp_path: Path, renderer: StubRenderer
) -> None:
    """A render killed part-way leaves a zero-byte file; the queue must re-render it."""
    params = parse_params(TEXT)
    directory = tmp_path / "videos"
    directory.mkdir(parents=True)
    leftover = directory / f"{cache.cache_key(params, **PINNED)}-preview.mp4"
    leftover.touch()
    queue = make_queue(tmp_path, renderer)
    queue.start()
    try:
        job, was_cached = queue.submit(params)
        assert was_cached is False
        assert wait_for(queue, job.id)["status"] == jobs.DONE
        assert renderer.texts() == [TEXT["text"]]
        assert leftover.read_bytes() == b"stub video bytes"
    finally:
        queue.stop()


# ---------------------------------------------------------- startup and shutdown


def test_start_creates_the_cache_directory(tmp_path: Path) -> None:
    target = tmp_path / "videos"
    queue = jobs.JobQueue(target, key_versions=PINNED)
    queue.start()
    queue.stop()
    assert target.is_dir()


def test_start_sweeps_videos_older_than_seven_days(tmp_path: Path) -> None:
    """SPEC.md section 6: the seven-day cleanup runs on startup."""
    target = tmp_path / "videos"
    target.mkdir()
    stale = target / f"{'a' * 64}-preview.mp4"
    stale.write_bytes(b"stale")
    long_ago = time.time() - 8 * 24 * 3600
    os.utime(stale, (long_ago, long_ago))
    recent = target / f"{'b' * 64}-preview.mp4"
    recent.write_bytes(b"fresh")

    queue = jobs.JobQueue(target, key_versions=PINNED)
    removed = queue.start()
    queue.stop()
    assert removed == [stale]
    assert not stale.exists()
    assert recent.is_file()


def test_stop_is_safe_when_the_queue_never_ran(tmp_path: Path) -> None:
    jobs.JobQueue(tmp_path / "videos", key_versions=PINNED).stop()


# --------------------------------------------------------------- the endpoints


def test_post_render_returns_202_and_a_job_id(app: Any) -> None:
    status, body = asgi_request(app, "POST", "/api/render", {"text": "Hello"})
    assert status == 202
    assert body == {"job_id": body["job_id"], "cached": False}
    assert jobs.JOB_ID_PATTERN.fullmatch(body["job_id"])


def test_a_render_can_be_polled_to_done_through_the_api(app: Any) -> None:
    status, submit, poll = submit_and_wait(app, {"text": "Poll me"})
    assert status == 202
    assert submit["cached"] is False
    assert poll["job_id"] == submit["job_id"]
    assert poll["status"] == jobs.DONE
    assert poll["error"] is None
    assert poll["video_url"].startswith(jobs.VIDEO_URL_PREFIX)
    assert cache.VIDEO_NAME_PATTERN.fullmatch(poll["video_url"].rsplit("/", 1)[-1])


def test_the_second_identical_request_is_cached(app: Any) -> None:
    """The ROADMAP 2.3 acceptance rule, stated as an HTTP exchange."""
    _, _, rendered = submit_and_wait(app, {"text": "Twice"})
    assert rendered["status"] == jobs.DONE
    started = time.monotonic()
    status, second = asgi_request(app, "POST", "/api/render", {"text": "Twice"})
    assert status == 202
    assert second["cached"] is True
    assert time.monotonic() - started < 1.0, "a cache hit must not wait for a render"
    _, replay = asgi_request(app, "GET", f"/api/jobs/{second['job_id']}")
    assert replay["status"] == jobs.DONE
    assert replay["video_url"] == rendered["video_url"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"text": "x" * 101}, "text_too_long"),
        ({"text": ""}, "text_empty"),
        ({"text": "a\nb\nc\nd"}, "too_many_lines"),
        ({"text": "Hi\r\tx"}, "text_control_character"),
        ({"text": "Hi", "text_color": "red"}, "text_color_invalid"),
        ({"text": "Hi", "outline_color": "#FFF"}, "outline_color_invalid"),
        ({"text": "Hi", "animation": "explode"}, "animation_invalid"),
        ({"text": "Hi", "font": "Comic Sans MS"}, "font_invalid"),
        ({"text": "Hi", "font_size": 500}, "font_size_out_of_range"),
        ({"text": "Hi", "font_size": "96"}, "font_size_invalid"),
        ({"text": "Hi", "animation_duration": "2"}, "animation_duration_invalid"),
        ({"text": "Hi", "hold_duration": True}, "hold_duration_invalid"),
        ({"text": "Hi", "outline_width": -1}, "outline_width_out_of_range"),
        ({"text": "Hi", "animation_duration": 15}, "animation_duration_out_of_range"),
        ({"text": "Hi", "quality": "4k"}, "quality_invalid"),
        ({"text": "Hi", "background": {"type": "transparent"}}, "background_type_unsupported"),
        ({"text": "Hi", "nope": 1}, "params_field_unknown"),
        (["not", "an", "object"], "params_not_object"),
        ("just a string", "params_not_object"),
    ],
)
def test_an_invalid_payload_is_422_with_a_stable_code(app: Any, body: Any, code: str) -> None:
    """The ROADMAP 2.3 acceptance rule: 422, an error ``code``, and a short English message."""
    status, payload = asgi_request(app, "POST", "/api/render", body)
    assert status == 422
    error = payload["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert "\n" not in error["message"]
    assert set(error) == {"code", "message", "params"}
    assert json.dumps(payload, allow_nan=False)


def test_a_body_that_is_not_json_is_422(app: Any) -> None:
    status, payload = asgi_request(app, "POST", "/api/render", raw_body=b"{not json")
    assert status == 422
    assert payload["error"]["code"] == "params_not_json"


def test_an_empty_body_is_422(app: Any) -> None:
    status, payload = asgi_request(app, "POST", "/api/render", raw_body=b"")
    assert status == 422
    assert payload["error"]["code"] == "params_not_json"


def test_an_empty_object_is_a_valid_render_request(app: Any) -> None:
    """SPEC.md section 5 gives every field a default, so {} means the default render."""
    _, _, poll = submit_and_wait(app, {})
    assert poll["status"] == jobs.DONE
    assert RenderParams().model_dump()["text"] == "Hello World"


def test_user_text_never_appears_in_an_error_response(app: Any) -> None:
    hostile = "Robert'); DROP TABLE x; <script>alert(1)</script>"
    status, payload = asgi_request(app, "POST", "/api/render", {"text": hostile * 4})
    assert status == 422
    assert hostile not in json.dumps(payload)
    assert payload["error"]["code"] == "text_too_long"


def test_an_unknown_job_is_404_with_a_stable_code(app: Any) -> None:
    status, payload = asgi_request(app, "GET", f"/api/jobs/{'f' * 32}")
    assert status == 404
    assert payload["error"]["code"] == "job_not_found"
    assert payload["error"]["message"]


@pytest.mark.parametrize("hostile", ["abc", "Z" * 32, "0" * 31, "not-a-job-id"])
def test_a_malformed_job_id_is_404_not_a_crash(app: Any, hostile: str) -> None:
    status, payload = asgi_request(app, "GET", f"/api/jobs/{hostile}")
    assert status == 404
    assert payload["error"]["code"] == "job_not_found"


def test_a_valid_request_that_cannot_be_keyed_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Manim version means no cache key, so the render must be refused, not guessed."""
    monkeypatch.setattr(
        cache.check_env,
        "check_manim",
        lambda: cache.check_env.CheckResult(name="Manim CE", ok=False, detail="missing"),
    )
    application = api_main.create_app(job_queue=jobs.JobQueue(tmp_path / "videos"))
    status, payload = asgi_request(application, "POST", "/api/render", {"text": "No manim"})
    assert status == 503
    assert payload["error"]["code"] == "manim_missing"


def test_the_ui_never_has_to_wait_for_a_render(app: Any, renderer: StubRenderer) -> None:
    """POST returns while the worker is still busy, which is why the job exists."""
    renderer.gate = threading.Event()
    _, submit = asgi_request(app, "POST", "/api/render", {"text": "Slow"})
    started = time.monotonic()
    status, poll = asgi_request(app, "GET", f"/api/jobs/{submit['job_id']}")
    assert status == 200
    assert time.monotonic() - started < 1.0
    assert poll["status"] in {jobs.QUEUED, jobs.RUNNING}
    assert renderer.gate is not None
    renderer.gate.set()


def test_the_asgi_lifespan_prepares_and_releases_the_queue(tmp_path: Path) -> None:
    """main.py wires startup and shutdown, so uvicorn gets both without extra code."""
    renderer = StubRenderer()
    job_queue = jobs.JobQueue(tmp_path / "videos", renderer=renderer, key_versions=PINNED)
    application = api_main.create_app(job_queue=job_queue)

    async def cycle() -> tuple[bool, dict[str, Any]]:
        async with application.router.lifespan_context(application):
            prepared = job_queue.output_dir.is_dir()
            job, _ = job_queue.submit(parse_params(TEXT))
            return prepared, wait_for(job_queue, job.id)

    prepared, payload = asyncio.run(cycle())
    assert prepared, "startup must create the cache directory"
    assert payload["status"] == jobs.DONE
    wait_for_worker_exit()


def test_the_app_exposes_its_queue_on_app_state(app: Any) -> None:
    """ROADMAP 2.4 needs the same queue instance to resolve a video path."""
    assert isinstance(app.state.jobs, jobs.JobQueue)
    assert app.state.jobs.output_dir.parts[-1] == data_dir.VIDEO_DIR_NAME


# ------------------------------------------------------ one real render, end to end


def test_the_queue_really_renders_a_preview_clip(tmp_path: Path) -> None:
    """No stub: the queue calls Manim, waits, and reports a playable MP4 at both stages."""
    job_queue = jobs.JobQueue(tmp_path / "videos")
    application = api_main.create_app(job_queue=job_queue)
    params = {
        "text": "Real queue",
        "animation": "fade_in",
        "animation_duration": 0.5,
        "hold_duration": 0.2,
        "quality": "preview",
    }
    started = time.monotonic()
    try:
        status, submit, poll = submit_and_wait(application, params, timeout=120.0)
        assert status == 202
        assert submit["cached"] is False
        assert poll["status"] == jobs.DONE, poll["error"]

        name = poll["video_url"].rsplit("/", 1)[-1]
        video = tmp_path / "videos" / name
        assert video.is_file()
        assert video.stat().st_size > 1000
        assert time.monotonic() - started < 120.0, "a preview must beat its own timeout"

        with av.open(str(video)) as container:
            stream = container.streams.video[0]
            assert (stream.width, stream.height) == (854, 480)
            assert float(stream.average_rate) == pytest.approx(15.0)
            frames = [frame.to_ndarray(format="rgb24") for frame in container.decode(stream)]
        assert len(frames) >= 5
        assert int((frames[-1].min(axis=2) > 200).sum()) > 500, "the text must be drawn by the end"

        _, replay = asgi_request(application, "POST", "/api/render", params)
        assert replay["cached"] is True
        _, replay_poll = asgi_request(application, "GET", f"/api/jobs/{replay['job_id']}")
        assert replay_poll["video_url"] == poll["video_url"]
    finally:
        job_queue.stop()
