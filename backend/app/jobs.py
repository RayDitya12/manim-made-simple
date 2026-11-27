"""Render jobs: one worker, FIFO, cache first (ROADMAP task 2.3, SPEC.md section 6).

SPEC.md section 6 fixes what this module has to do:

*   jobs run in this process, one render at a time, in the order they arrived. No Redis,
    no Celery, no thread pool.
*   ``POST /api/render`` answers ``202 {job_id, cached}``. When the video already exists
    on disk the job is created already ``done`` and nothing is queued, which is what makes
    the second identical request answer instantly.
*   ``GET /api/jobs/{id}`` answers ``queued | running | done | failed``, with ``video_url``
    once the file exists and a stable error code when the render did not produce one.

The worker is a single thread that pulls job ids from a :class:`queue.Queue` and calls
:func:`backend.app.render.render_video`, which is blocking by design: one Manim child
process at a time keeps the app responsive on a laptop and keeps CPU spikes away from the
user's other work. The endpoint handlers therefore never wait for a render; they record a
job and return.

Two rules are worth stating because they are easy to get wrong:

*   A finished job's ``video_url`` points at ``/api/videos/<name>``, the endpoint that
    ships with ROADMAP 2.4. The name comes from the cache key, so it is 64 hex characters,
    a quality id and an extension -- never a fragment of the user's text.
*   The worker re-checks the cache before it renders. Two requests for the same parameters
    made while the first is still running therefore queue two jobs, but only the first one
    starts Manim; the second finds the finished file and completes. That is cheaper and
    simpler than tracking in-flight keys.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app import cache
from backend.app.models import RenderParams, RenderParamsError
from backend.app.render import OUTPUT_EXTENSION, RenderError, render_video

logger = logging.getLogger("mms.jobs")

#: The four SPEC.md section 6 statuses.
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STATUSES: tuple[str, ...] = (QUEUED, RUNNING, DONE, FAILED)

#: Job ids are generated here, so a lookup may assume this shape (SPEC.md section 10).
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: Where the UI fetches a finished video. ROADMAP 2.4 serves this path.
VIDEO_URL_PREFIX = "/api/videos/"

#: How long :meth:`JobQueue.stop` waits for a render already in progress before it gives
#: up and lets the process exit. A Manim render is killed by the app shell (ROADMAP 6.1);
#: the queue only avoids hanging a quit on it.
STOP_JOIN_SECONDS = 2.0

#: How often an idle worker re-checks the quit flag, so stopping does not need a queue
#: message that a later restart would then swallow and die on.
WORKER_POLL_SECONDS = 0.1


def video_url(file_name: str) -> str:
    """The URL the UI plays or downloads for one cached file name."""
    return f"{VIDEO_URL_PREFIX}{file_name}"


@dataclass
class Job:
    """One render request and everything the API reports about it."""

    id: str
    params: RenderParams
    cache_key: str
    stem: str
    status: str = QUEUED
    video_url: str | None = None
    error: dict[str, Any] | None = None

    @property
    def file_name(self) -> str:
        """``<sha256>-<quality>.<ext>``, the name the video has on disk and in its URL."""
        return f"{self.stem}.{OUTPUT_EXTENSION}"

    def payload(self) -> dict[str, Any]:
        """The body of ``GET /api/jobs/{id}`` (SPEC.md section 6)."""
        return {
            "job_id": self.id,
            "status": self.status,
            "video_url": self.video_url,
            "error": self.error,
        }


class JobQueue:
    """An in-process FIFO render queue with a single worker thread."""

    def __init__(
        self,
        output_dir: Path,
        *,
        renderer: Callable[[dict[str, Any], Path, str], Path] = render_video,
        key_versions: Mapping[str, str] | None = None,
    ) -> None:
        """``renderer`` and ``key_versions`` exist so a test can drive the queue without
        waiting on Manim; production uses the real renderer and this machine's versions.
        """
        self.output_dir = Path(output_dir)
        self._renderer = renderer
        self._key_versions = dict(key_versions or {})
        self._pending: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._quit = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> list[Path]:
        """Prepare the output directory, drop stale videos, raise the worker.

        SPEC.md section 6 asks for the seven-day cleanup on startup; doing it here means
        the app gets it whether it boots through the ASGI lifespan or a caller starts the
        queue directly. Returns the files removed.
        """
        logger.info("render cache in %s", self.output_dir)
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create %s: %s", self.output_dir, exc)
        removed = cache.cleanup_stale_videos(self.output_dir)
        if removed:
            logger.info("cleared %d stale video(s) from the cache", len(removed))
        self._ensure_worker()
        return removed

    def stop(self, timeout: float = STOP_JOIN_SECONDS) -> None:
        """Ask the worker to finish the render in hand and leave the loop.

        A render already running is not interrupted here: the queue's job is the queue, and
        killing the child process belongs to the app shell (ROADMAP 6.1).
        """
        with self._lock:
            thread = self._thread
            self._thread = None
        self._quit.set()
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("the render worker is still busy after %s s", timeout)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._quit.clear()
            self._thread = threading.Thread(target=self._loop, name="mms-jobs", daemon=True)
            self._thread.start()

    # -------------------------------------------------------------- submit and poll

    def submit(self, params: RenderParams) -> tuple[Job, bool]:
        """Record a job for ``params`` and return it with whether the file already existed.

        A cached render is reported ``done`` immediately and never queued, so the caller can
        answer ``202 {job_id, cached: true}`` without waiting. ``cache.CacheError`` (an
        unknowable Manim version) propagates: the request cannot be keyed, so it must not be
        accepted.
        """
        key = cache.cache_key(params, **self._key_versions)
        job = Job(
            id=uuid.uuid4().hex,
            params=params,
            cache_key=key,
            stem=cache.video_stem(key, params.quality),
        )
        hit = cache.is_cached(self._path(job))
        with self._lock:
            self._jobs[job.id] = job
            if hit:
                job.status = DONE
                job.video_url = video_url(job.file_name)
        if not hit:
            self._ensure_worker()
            self._pending.put(job.id)
        return job, hit

    def payload(self, job_id: str) -> dict[str, Any] | None:
        """The current status body for ``job_id``, or ``None`` when there is no such job."""
        if not JOB_ID_PATTERN.fullmatch(job_id):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else job.payload()

    def _path(self, job: Job) -> Path:
        return cache.video_path(self.output_dir, job.cache_key, job.params.quality)

    # ----------------------------------------------------------------- the worker

    def _loop(self) -> None:
        while not self._quit.is_set():
            try:
                job_id = self._pending.get(timeout=WORKER_POLL_SECONDS)
            except queue.Empty:
                continue
            self._run(job_id)

    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status != QUEUED:
                return
            job.status = RUNNING
        logger.info("job %s rendering (%s quality)", job_id, job.params.quality)
        try:
            produced = self._produce(job)
        except (RenderParamsError, RenderError, cache.CacheError) as exc:
            self._fail(job, exc.code, exc.message, exc.params)
        except Exception:
            # One broken render must not take the worker, and so every later job, with it.
            logger.exception("job %s failed for a reason the render layer did not name", job_id)
            self._fail(job, "render_failed", "the video could not be rendered.")
        else:
            with self._lock:
                job.status = DONE
                job.video_url = video_url(produced.name)
            logger.info("job %s done: %s", job_id, produced)

    def _produce(self, job: Job) -> Path:
        """Return the video for ``job``, rendering it only when the cache has nothing."""
        path = self._path(job)
        if cache.is_cached(path):
            logger.info("job %s served from the cache", job.id)
            return path
        return self._renderer(job.params.model_dump(), self.output_dir, job.stem)

    def _fail(
        self, job: Job, code: str, message: str, params: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            job.status = FAILED
            job.error = {"code": code, "message": message, "params": dict(params or {})}


__all__ = [
    "JOB_ID_PATTERN",
    "STATUSES",
    "QUEUED",
    "RUNNING",
    "DONE",
    "FAILED",
    "STOP_JOIN_SECONDS",
    "VIDEO_URL_PREFIX",
    "WORKER_POLL_SECONDS",
    "Job",
    "JobQueue",
    "video_url",
]
