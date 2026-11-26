"""Tests for the FastAPI app shell: /api/health and /api/options (ROADMAP 2.1).

The requests are driven through the ASGI interface with a small helper instead of
``fastapi.testclient``, which would add ``httpx`` as a new dependency for no benefit
here: both endpoints return a plain dictionary, so there is no response model, status
selection, or header worth exercising.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import manim
import pytest

from backend.app import app_meta
from backend.app import main as api_main
from backend.app.models import FONTS, RenderParams, RenderParamsError, parse_params

REPO_ROOT = Path(__file__).resolve().parents[2]


def asgi_get(app: Any, path: str) -> tuple[int, Any]:
    """Send one GET through the app's ASGI interface and return (status, decoded body)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1")],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    status: dict[str, int] = {}
    body = bytearray()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status.get("code", 0), json.loads(bytes(body) or b"null")


def ffmpeg_result(
    *,
    ok: bool,
    warn_only: bool = False,
    version: str = "",
    source: str = "",
) -> Any:
    """A stand-in for one ``check_env.check_ffmpeg()`` outcome."""
    return api_main.check_env.CheckResult(
        name="FFmpeg",
        ok=ok,
        detail="",
        warn_only=warn_only,
        version=version,
        source=source,
    )


@pytest.fixture(scope="module")
def client() -> Any:
    return api_main.create_app()


@pytest.fixture(scope="module")
def health(client: Any) -> dict[str, Any]:
    status, body = asgi_get(client, "/api/health")
    assert status == 200
    return body


@pytest.fixture(scope="module")
def options(client: Any) -> dict[str, Any]:
    status, body = asgi_get(client, "/api/options")
    assert status == 200
    return body


# ------------------------------------------------------------------- /api/health


def test_health_names_the_versions_in_use(health: dict[str, Any]) -> None:
    """SPEC.md section 6: health carries the manim, ffmpeg, and python versions."""
    assert health["manim_version"] == manim.__version__
    assert health["python_version"] == ".".join(str(part) for part in sys.version_info[:3])


def test_health_is_ok_on_a_working_machine(health: dict[str, Any]) -> None:
    assert health["status"] == "ok"
    assert health["error"] is None
    # This machine has no ffmpeg CLI, yet rendering works through PyAV, so health says ok.
    assert health["ffmpeg_version"]
    assert health["ffmpeg_source"] in {"cli", "pyav"}


def test_health_carries_only_the_spec_version_fields(health: dict[str, Any]) -> None:
    """SPEC.md section 6 lists four fields; the app identity lives in /api/options."""
    assert {"status", "python_version", "manim_version", "ffmpeg_version"} <= set(health)
    assert "app_name" not in health


def test_health_names_a_missing_manim_with_a_stable_code() -> None:
    def missing() -> str:
        raise ImportError("no module named 'manim'")

    payload = api_main.health_payload(manim=missing)
    assert payload["status"] == "degraded"
    assert payload["manim_version"] is None
    assert payload["error"]["code"] == "manim_missing"
    assert payload["error"]["message"]


def test_health_names_a_missing_ffmpeg_with_a_stable_code() -> None:
    unavailable = {"available": False, "version": None, "source": None}
    payload = api_main.health_payload(ffmpeg=lambda: unavailable)
    assert payload["status"] == "degraded"
    assert payload["ffmpeg_version"] is None
    assert payload["error"]["code"] == "ffmpeg_missing"


def test_pyav_libraries_count_as_an_available_ffmpeg() -> None:
    info = api_main.ffmpeg_info(
        lambda: ffmpeg_result(ok=False, warn_only=True, version="6.1.1", source="pyav")
    )
    assert info == {"available": True, "version": "6.1.1", "source": "pyav"}


def test_cli_ffmpeg_is_reported_by_version_and_source() -> None:
    info = api_main.ffmpeg_info(lambda: ffmpeg_result(ok=True, version="7.0.2", source="cli"))
    assert info["version"] == "7.0.2"
    assert info["source"] == "cli"


def test_ffmpeg_with_neither_cli_nor_pyav_is_unavailable() -> None:
    assert api_main.ffmpeg_info(lambda: ffmpeg_result(ok=False, warn_only=True)) == {
        "available": False,
        "version": None,
        "source": None,
    }


# ------------------------------------------------------------------ /api/options


def test_options_identifies_the_app_from_the_single_meta_file(options: dict[str, Any]) -> None:
    raw = json.loads((REPO_ROOT / app_meta.META_FILE_NAME).read_text(encoding="utf-8"))
    assert options["app"] == {"name": raw["name"], "slug": raw["slug"]}


def test_options_lists_the_three_animations(options: dict[str, Any]) -> None:
    assert options["animations"] == ["write", "fade_in", "typewriter"]


def test_options_lists_the_font_whitelist_in_order(options: dict[str, Any]) -> None:
    assert options["fonts"] == list(FONTS)
    assert options["defaults"]["font"] == options["fonts"][0]


def test_options_quality_presets_match_the_render_presets(options: dict[str, Any]) -> None:
    assert [quality["id"] for quality in options["qualities"]] == ["preview", "hd"]
    assert [(q["width"], q["height"], q["fps"]) for q in options["qualities"]] == [
        (854, 480, 15),
        (1920, 1080, 60),
    ]
    assert all(quality["extension"] == "mp4" for quality in options["qualities"])
    assert all(
        quality["resolution"] == f"{quality['width']}x{quality['height']}"
        for quality in options["qualities"]
    )


def test_options_limits_are_the_spec_numbers(options: dict[str, Any]) -> None:
    limits = options["limits"]
    assert limits["text"] == {"min_chars": 1, "max_chars": 100, "max_lines": 3}
    assert limits["animation_duration"] == {"min": 0.5, "max": 10.0}
    assert limits["hold_duration"] == {"min": 0.0, "max": 10.0}
    assert limits["font_size"] == {"min": 24, "max": 200}
    assert limits["outline_width"] == {"min": 0.0, "max": 10.0}
    assert limits["total_duration_max"] == 20.0
    assert options["hex_color_pattern"] == "^#[0-9A-Fa-f]{6}$"


def test_options_defaults_are_the_spec_defaults(options: dict[str, Any]) -> None:
    assert options["defaults"] == RenderParams().model_dump()
    assert options["defaults"] == {
        "text": "Hello World",
        "animation": "write",
        "animation_duration": 2.0,
        "hold_duration": 1.0,
        "font": "Inter",
        "font_size": 96,
        "text_color": "#FFFFFF",
        "outline_color": "#000000",
        "outline_width": 0.0,
        "background": {"type": "solid", "color": "#000000"},
        "quality": "preview",
    }


def test_options_defers_the_transparent_background(options: dict[str, Any]) -> None:
    """SPEC.md section 5 makes transparent output available from Phase 4, not now."""
    assert {item["id"]: item["available"] for item in options["background_types"]} == {
        "solid": True,
        "transparent": False,
    }
    with pytest.raises(RenderParamsError) as excinfo:
        parse_params({**options["defaults"], "background": {"type": "transparent"}})
    assert excinfo.value.code == "background_type_unsupported"


def test_options_body_is_strict_json(options: dict[str, Any]) -> None:
    json.dumps(options, allow_nan=False)


# --------------------------------------------- the advertised limits are the real ones


def test_every_advertised_default_is_accepted(options: dict[str, Any]) -> None:
    assert parse_params(options["defaults"]).model_dump() == options["defaults"]


def test_values_outside_the_advertised_limits_are_rejected(options: dict[str, Any]) -> None:
    """Whatever the UI builds from /api/options is what the backend enforces."""
    limits = options["limits"]
    too_long_total = limits["animation_duration"]["max"] + limits["hold_duration"]["max"]
    cases: dict[str, Any] = {
        "text": "x" * (limits["text"]["max_chars"] + 1),
        "font_size": limits["font_size"]["max"] + 1,
        "outline_width": limits["outline_width"]["max"] + 0.5,
        # Legal on its own, but it breaks the total-duration cap the endpoint publishes.
        "animation_duration": too_long_total,
    }
    for field, value in cases.items():
        with pytest.raises(RenderParamsError) as excinfo:
            parse_params({**options["defaults"], field: value})
        assert excinfo.value.code.startswith(field), field


def test_only_advertised_fonts_are_accepted(options: dict[str, Any]) -> None:
    for font in options["fonts"]:
        assert parse_params({**options["defaults"], "font": font}).font == font
    with pytest.raises(RenderParamsError) as excinfo:
        parse_params({**options["defaults"], "font": "Comic Sans MS"})
    assert excinfo.value.code == "font_invalid"


def test_only_advertised_animations_are_accepted(options: dict[str, Any]) -> None:
    for animation in options["animations"]:
        params = parse_params({**options["defaults"], "animation": animation})
        assert params.animation == animation
    with pytest.raises(RenderParamsError) as excinfo:
        parse_params({**options["defaults"], "animation": "explode"})
    assert excinfo.value.code == "animation_invalid"


def test_only_advertised_qualities_are_accepted(options: dict[str, Any]) -> None:
    for quality in options["qualities"]:
        params = parse_params({**options["defaults"], "quality": quality["id"]})
        assert params.quality == quality["id"]
    with pytest.raises(RenderParamsError) as excinfo:
        parse_params({**options["defaults"], "quality": "4k"})
    assert excinfo.value.code == "quality_invalid"


# ------------------------------------------------------------------------- routing


def test_endpoints_are_looked_up_under_the_api_prefix(client: Any) -> None:
    assert asgi_get(client, "/api/health")[0] == 200
    assert asgi_get(client, "/api/options")[0] == 200


def test_unknown_and_unprefixed_paths_are_not_found(client: Any) -> None:
    assert asgi_get(client, "/health")[0] == 404
    assert asgi_get(client, "/api/render")[0] == 404
    assert asgi_get(client, "/api/nope")[0] == 404


def test_post_is_not_allowed_on_a_read_only_endpoint(client: Any) -> None:
    """POST /api/render arrives with ROADMAP 2.3; nothing accepts a body yet."""

    async def call() -> int:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/health",
            "raw_path": b"/api/health",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 54321),
            "server": ("127.0.0.1", 8000),
        }
        status: dict[str, int] = {}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])

        await api_main.create_app()(scope, receive, send)
        return status.get("code", 0)

    assert asyncio.run(call()) == 405


# ------------------------------------------------------------------------ app_meta


def test_app_meta_reads_the_single_source_of_the_name() -> None:
    raw = json.loads((REPO_ROOT / "app_meta.json").read_text(encoding="utf-8"))
    assert app_meta.app_name() == raw["name"]
    assert app_meta.app_slug() == raw["slug"]
    assert app_meta.app_version() == raw["version"]


def test_short_form_is_never_the_public_name() -> None:
    """SPEC.md section 3: "MMS" is for developer docs only, never user-facing text."""
    assert "MMS" not in app_meta.app_name()
    assert app_meta.app_name() == "Manim Made Simple"


def test_app_meta_falls_back_when_the_file_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(app_meta, "_candidate_paths", lambda: (REPO_ROOT / "absent.json",))
    app_meta._load.cache_clear()
    try:
        assert app_meta.app_name() == app_meta.FALLBACK_NAME
        assert app_meta.app_slug() == app_meta.FALLBACK_SLUG
        assert app_meta.app_version() == app_meta.FALLBACK_VERSION
    finally:
        app_meta._load.cache_clear()


def test_app_meta_survives_a_malformed_file(monkeypatch, tmp_path) -> None:
    broken = tmp_path / "app_meta.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(app_meta, "_candidate_paths", lambda: (broken,))
    app_meta._load.cache_clear()
    try:
        assert app_meta.app_version() == app_meta.FALLBACK_VERSION
    finally:
        app_meta._load.cache_clear()
