"""The FastAPI app: metadata endpoints for ROADMAP task 2.1 (SPEC.md section 6).

Two endpoints live here so far:

``GET /api/health``
    Reports whether this machine can render, plus the interpreter, Manim and
    FFmpeg versions Electron polls before it opens the window.

``GET /api/options``
    Reports every allowed value and limit from SPEC.md section 5. The UI builds its
    controls from this response instead of repeating the numbers, so a limit changed
    here is the only change needed.

FFmpeg follows the rule established by ``scripts/check_env.py``: a missing
``ffmpeg`` command line binary is a warning, not a failure, because Manim CE 0.22.1
encodes through PyAV, whose wheel bundles the FFmpeg libraries. The versions come
from the installed packages at request time rather than from the lockfile, so a
report that names a version is a report about the interpreter that is really running.

The render endpoints (ROADMAP 2.3), the video endpoint (2.4) and the host/CORS
hardening (2.5) are deliberately not here yet; this task is the app shell only.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from backend.app import app_meta
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
)
from backend.app.render import OUTPUT_EXTENSION, QUALITY_PRESETS
from scripts import check_env

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
        error = _health_error("manim_missing", "Manim is not installed in this Python.")
    elif not ffmpeg_value["available"]:
        error = _health_error(
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


def _health_error(code: str, message: str) -> dict[str, Any]:
    """The SPEC.md section 6 error object: a stable code plus an English fallback."""
    return {"code": code, "message": message, "params": {}}


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


def create_app() -> FastAPI:
    """Build the backend app."""
    app = FastAPI(
        title=app_meta.app_name(),
        version=app_meta.app_version(),
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return health_payload()

    @app.get("/api/options")
    def options() -> dict[str, Any]:
        return options_payload()

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
    print(f"{app_meta.app_name()} backend on http://{host}:{port} (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKGROUND_TYPES",
    "app",
    "create_app",
    "ffmpeg_info",
    "health_payload",
    "main",
    "manim_version",
    "options_payload",
    "python_version",
]
