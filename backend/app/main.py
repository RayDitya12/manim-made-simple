"""The FastAPI app: the API contract of SPEC.md section 6.

Endpoints so far:

``GET /api/health``
    Reports whether this machine can render, plus the interpreter, Manim and
    FFmpeg versions Electron polls before it opens the window.

``GET /api/options``
    Reports every allowed value and limit from SPEC.md section 5. The UI builds its
    controls from this response instead of repeating the numbers, so a limit changed
    here is the only change needed.

``POST /api/render``
    Validates the parameters, hands them to the job queue (ROADMAP 2.3) and answers
    ``202 {job_id, cached}`` without waiting for the render.

``GET /api/jobs/{job_id}``
    Reports ``queued | running | done | failed``, the ``video_url`` when the file exists,
    and the stable error code when the render failed.

``GET /api/videos/{name}``
    Streams one file out of the render cache (ROADMAP 2.4). The name has to be one this app
    produced, and ``?download=1`` asks for it as an attachment instead of inline.

FFmpeg follows the rule established by ``scripts/check_env.py``: a missing
``ffmpeg`` command line binary is a warning, not a failure, because Manim CE 0.22.1
encodes through PyAV, whose wheel bundles the FFmpeg libraries. The versions come
from the installed packages at request time rather than from the lockfile, so a
report that names a version is a report about the interpreter that is really running.

The app owns one :class:`~backend.app.jobs.JobQueue`, whose cache lives in the data
directory of :mod:`backend.app.data_dir`. File serving is
:mod:`backend.app.videos`; the host/CORS hardening (2.5) is deliberately not here yet.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from backend.app import app_meta, cache, data_dir, jobs, videos
from backend.app.models import (
    ANIMATIONS,
    FONTS,
    HEX_COLOR_PATTERN,
    NUMERIC_RANGES,
    SOLID,
    TEXT_MAX_CHARS,
    TEXT_MAX_LINES,
    TOTAL_DURATION_MAX,
    TRANSPARENT,
    RenderParams,
    RenderParamsError,
    parse_params,
)
from backend.app.render import OUTPUT_EXTENSION, QUALITY_PRESETS
from scripts import check_env

logger = logging.getLogger("mms.api")

#: Background types the UI may offer. ``transparent`` is listed as unavailable until
#: ROADMAP 4.1 renders it, so the option appears in the API without being selectable.
BACKGROUND_TYPES: tuple[dict[str, Any], ...] = (
    {"id": SOLID, "available": True},
    {"id": TRANSPARENT, "available": False},
)


def python_version() -> str:
    """The version of the interpreter running the backend."""
    return ".".join(str(part) for part in sys.version_info[:3])


def manim_version() -> str:
    """The installed Manim CE version.

    Raises :class:`ImportError` when Manim is absent, which is what makes
    ``/api/health`` report ``degraded`` instead of naming a version that is not there.
    """
    import manim

    return str(getattr(manim, "__version__", "unknown"))


def ffmpeg_info(check_ffmpeg: Callable[[], check_env.CheckResult] | None = None) -> dict[str, Any]:
    """Classify the FFmpeg situation the way ``scripts/check_env.py`` does.

    A missing ``ffmpeg`` binary is still usable when PyAV supplies the libraries, so
    ``available`` follows the version found rather than ``ok`` alone.
    """

    result = (check_ffmpeg or check_env.check_ffmpeg)()
    if not result.version:
        return {"available": False, "version": None, "source": None}
    return {"available": True, "version": result.version, "source": result.source}


def health_payload(
    *,
    python: Callable[[], str] = python_version,
    manim: Callable[[], str] = manim_version,
    ffmpeg: Callable[[], dict[str, Any]] = ffmpeg_info,
) -> dict[str, Any]:
    """Assemble the ``/api/health`` body from the three environment probes.

    SPEC.md section 6 defines the shape as ``{status, python_version, manim_version,
    ffmpeg_version, ffmpeg_source, error}``. When a component is missing its version is
    ``null`` rather than a guess and ``error`` carries one of the stable codes below, so
    the UI can show a translated sentence instead of a stack trace. The app's name is not
    repeated here: ``/api/options`` carries it.
    """
    try:
        manim_value: str | None = manim()
    except ImportError:
        manim_value = None
    ffmpeg_value = ffmpeg()
    if manim_value is None:
        error = error_object("manim_missing", "Manim is not installed in this Python.")
    elif not ffmpeg_value["available"]:
        error = error_object(
            "ffmpeg_missing",
            "no FFmpeg was found, not even the one bundled with PyAV.",
        )
    else:
        error = None
    return {
        "status": "ok" if error is None else "degraded",
        "python_version": python(),
        "manim_version": manim_value,
        "ffmpeg_version": ffmpeg_value["version"],
        "ffmpeg_source": ffmpeg_value["source"],
        "error": error,
    }


def error_object(code: str, message: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """The SPEC.md section 6 error object: a stable code plus an English fallback.

    ``params`` carries the numbers a translated sentence needs. No user input is put in a
    message, and none is echoed back here: the UI shows the translation of ``code``.
    """
    return {"code": code, "message": message, "params": dict(params or {})}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    params: dict[str, Any] | None = None,
) -> JSONResponse:
    """An error the API layer produces itself, in the same shape as a render failure."""
    return JSONResponse(
        status_code=status_code,
        content={"error": error_object(code, message, params)},
    )


def _range(low: float | int, high: float | int) -> dict[str, float | int]:
    return {"min": low, "max": high}


def options_payload() -> dict[str, Any]:
    """The SPEC.md section 5 schema as data: allowed values, limits, and defaults.

    Every number is read from :mod:`backend.app.models`, the same module that
    validates a render request, so what the UI offers and what the backend accepts
    cannot drift apart.
    """
    defaults = RenderParams().model_dump()
    return {
        "app": {"name": app_meta.app_name(), "slug": app_meta.app_slug()},
        "animations": list(ANIMATIONS),
        "fonts": list(FONTS),
        "qualities": [
            {
                "id": quality,
                "width": preset.width,
                "height": preset.height,
                "fps": preset.fps,
                "resolution": f"{preset.width}x{preset.height}",
                "extension": OUTPUT_EXTENSION,
            }
            for quality, preset in QUALITY_PRESETS.items()
        ],
        "background_types": list(BACKGROUND_TYPES),
        "limits": {
            "text": {
                "min_chars": 1,
                "max_chars": TEXT_MAX_CHARS,
                "max_lines": TEXT_MAX_LINES,
            },
            "animation_duration": _range(*NUMERIC_RANGES["animation_duration"]),
            "hold_duration": _range(*NUMERIC_RANGES["hold_duration"]),
            "font_size": _range(*NUMERIC_RANGES["font_size"]),
            "outline_width": _range(*NUMERIC_RANGES["outline_width"]),
            "total_duration_max": TOTAL_DURATION_MAX,
        },
        "hex_color_pattern": HEX_COLOR_PATTERN,
        "defaults": defaults,
    }


def create_app(job_queue: jobs.JobQueue | None = None) -> FastAPI:
    """Build the backend app.

    ``job_queue`` is injected by a test that wants a temporary cache directory and a stub
    renderer; production gets a queue whose videos live in the data directory of
    :mod:`backend.app.data_dir` (SPEC.md section 6).
    """
    render_queue = job_queue if job_queue is not None else jobs.JobQueue(data_dir.video_dir())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Startup prepares the cache and drops videos older than seven days; shutdown asks
        # the worker to stop so a quit does not leave a render thread behind.
        render_queue.start()
        try:
            yield
        finally:
            render_queue.stop()

    app = FastAPI(
        title=app_meta.app_name(),
        version=app_meta.app_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.jobs = render_queue

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return health_payload()

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return options_payload()

    @app.post("/api/render")
    async def render(request: Request) -> JSONResponse:
        """Accept a render request and answer ``202 {job_id, cached}`` (SPEC.md section 6).

        Nothing here waits for Manim: the parameters are validated, the cache is consulted,
        and the job is either already done or queued for the single worker.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_response(
                422, "params_not_json", "the request body must be a JSON object."
            )
        try:
            params = parse_params(body)
        except RenderParamsError as exc:
            return _error_response(422, exc.code, exc.message, exc.params)
        try:
            job, cached = render_queue.submit(params)
        except cache.CacheError as exc:
            # The machine cannot key a render, so it cannot render one either.
            return _error_response(503, exc.code, exc.message, exc.params)
        logger.info("%s job %s", "cached" if cached else "queued", job.id)
        return JSONResponse(
            status_code=202,
            content={"job_id": job.id, "cached": cached},
        )

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> JSONResponse:
        """Poll one job. An unknown or malformed id is a 404 with a stable code."""
        payload = render_queue.payload(job_id)
        if payload is None:
            return _error_response(404, "job_not_found", "no render job has this id.")
        return JSONResponse(status_code=200, content=payload)

    @app.api_route("/api/videos/{name}", methods=["GET", "HEAD"])
    def video_file(name: str, download: str | None = None) -> Response:
        """Stream one cached video (SPEC.md section 6).

        :func:`backend.app.videos.serve` rebuilds the path from the pieces of a name it
        matched completely, so this handler never receives a path derived from the request,
        and an unusable name comes back as a stable error code. ``HEAD`` is listed because
        a file endpoint that answers ``GET`` has to (RFC 9110 section 9.3.2), and
        ``FileResponse`` already sends the headers without the body.
        """
        try:
            return videos.serve(
                render_queue.output_dir,
                name,
                download=download == videos.DOWNLOAD_QUERY_VALUE,
            )
        except (videos.VideoError, cache.CacheError) as exc:
            return _error_response(videos.ERROR_STATUS.get(exc.code, 500), exc.code, exc.message)

    @app.api_route("/api/videos/{requested_path:path}", methods=["GET", "HEAD"])
    def video_name_with_slashes(requested_path: str) -> JSONResponse:
        """Answer a request that got this far with a name containing a slash.

        ``{name}`` matches one path segment, so a traversal attempt arrives as a longer path
        that route cannot match. Returning the SPEC.md section 6 error object here, rather
        than leaving it to the framework's bare 404, keeps one shape for every API error and
        makes the rejection testable. Nothing is resolved or stat'ed: the path is unread.
        """
        return _error_response(400, "video_name_invalid", videos.NAME_INVALID_MESSAGE)

    return app


app = create_app()


def main() -> int:
    """Run the backend for development: ``python -m backend.app.main``.

    Bound to ``127.0.0.1`` only (SPEC.md section 10). Electron starts its own
    backend on a random free port (ROADMAP 6.1), so this entry point is the terminal
    workflow from SPEC.md section 4, stage 1.
    """
    import uvicorn

    host = "127.0.0.1"
    port = 8000
    cache_dir = data_dir.describe()
    print(f"{app_meta.app_name()} backend on http://{host}:{port} (Ctrl+C to stop)")
    print(f"render cache: {cache_dir['video_dir']} (from {cache_dir['source']})")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKGROUND_TYPES",
    "app",
    "create_app",
    "error_object",
    "ffmpeg_info",
    "health_payload",
    "main",
    "manim_version",
    "options_payload",
    "python_version",
]
