"""Tests for the served interface (ROADMAP task 3.1, SPEC.md sections 8 and 10).

Two halves. The first is the file endpoint: which URLs answer, what headers they carry, and
that nothing outside ``ui/`` can be read through them. Requests go through the ASGI interface
directly, as in ``test_videos.py``, because an HTTP client normalises ``..`` out of a URL
before sending it and that is exactly the behaviour under test.

The second half is the copy contract, which no browser is needed for: the markup must hold no
hard-coded text, every key the markup and the scripts name must exist in both locale files,
the locale files must hold the same keys, and every address the UI asks for must be one the
backend actually serves. These are the rules SPEC.md section 8 states, enforced where they can
be enforced cheaply rather than remembered by the next person to edit a string.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from backend.app import app_meta, webui
from backend.app import main as api_main
from backend.app.models import ANIMATIONS, FONTS, QUALITIES, RenderParams

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_FILES = ("index.html", "app.js", "i18n.js", "styles.css")

#: Something only a file outside ``ui/`` could supply, to prove a rejected read stayed unread.
SECRET = "not-a-ui-file"


@pytest.fixture(scope="module")
def app() -> Any:
    return api_main.create_app()


@pytest.fixture(scope="module")
def html() -> str:
    return (REPO_ROOT / "ui" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scripts() -> str:
    return "\n".join(
        (REPO_ROOT / "ui" / name).read_text(encoding="utf-8") for name in ("app.js", "i18n.js")
    )


@pytest.fixture(scope="module")
def locales() -> dict[str, dict[str, str]]:
    return {
        name: json.loads((REPO_ROOT / "ui" / "locales" / f"{name}.json").read_text("utf-8"))
        for name in ("en", "id")
    }


class Receive:
    """A stub ``receive`` that behaves like uvicorn's around a ``FileResponse``.

    Answering instantly with ``http.disconnect`` would look like a client that hung up and
    cancel the body, so the stub reports the request once and then waits for the response.
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
            await asyncio.sleep(0.005)
        return {"type": "http.disconnect"}


def asgi_request(
    app: Any,
    path: str,
    *,
    method: str = "GET",
    headers: tuple[tuple[bytes, bytes], ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    """Send one request through the app and return (status, response headers, body)."""
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


# ------------------------------------------------------------------------ routing


def test_the_root_url_serves_the_interface(app: Any) -> None:
    status, headers, body = asgi_request(app, "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert body.decode("utf-8").lstrip().lower().startswith("<!doctype html>")


@pytest.mark.parametrize("url_path", sorted(webui.RESOURCES))
def test_every_advertised_url_serves_its_file(app: Any, url_path: str) -> None:
    status, headers, body = asgi_request(app, url_path)
    assert status == 200
    assert headers["content-type"] == webui.RESOURCES[url_path][1]
    assert body, url_path


@pytest.mark.parametrize("url_path", sorted(webui.RESOURCES))
def test_the_interface_files_are_all_present(url_path: str) -> None:
    file_path, _ = webui.path_for(url_path)
    assert file_path is not None and file_path.is_file()


def test_the_route_list_and_the_resource_table_are_the_same(app: Any) -> None:
    """A file added to the table is served, and nothing else is routed at the root."""
    served = {route.path for route in app.routes if getattr(route, "path", "") in webui.RESOURCES}
    assert served == set(webui.RESOURCES)


def test_post_is_not_a_way_to_read_the_interface(app: Any) -> None:
    assert asgi_request(app, "/app.js", method="POST")[0] == 405


def test_the_api_routes_are_untouched_by_the_interface(app: Any) -> None:
    """/health and /api/nope stay "no such endpoint" instead of falling back to the page."""
    assert asgi_request(app, "/health")[0] == 404
    assert asgi_request(app, "/api/nope")[0] == 404
    assert asgi_request(app, "/")[0] == 200


# ------------------------------------------------------------------------ traversal


@pytest.mark.parametrize(
    "path",
    [
        "/../requirements.txt",
        "/../../requirements.txt",
        "/%2e%2e/requirements.txt",
        "/..%2f..%2frequirements.txt",
        "/ui/app.js",
        "/ui/locales/en.json",
        "//app.js",
        "/app.js/..",
        "/locales/../app.js",
        "/locales/%2e%2e/app.js",
        f"/locales/{SECRET}.json",
        "/STYLES.CSS",
        "/requirements.txt",
        "/SPEC.md",
        "/app_meta.json",
        "/favicon.ico",
        "/index.html%00.css",
        "\\app.js",
    ],
)
def test_a_path_that_is_not_in_the_table_is_refused(app: Any, path: str) -> None:
    """Nothing outside the table is resolved, so nothing outside ``ui/`` is ever opened."""
    status, _, body = asgi_request(app, path)
    assert status == 404, path
    assert SECRET not in body.decode("utf-8", "replace")


@pytest.mark.parametrize("path", ["/app.js/", "/index.html/", "/locales/en.json/", "/styles.css//"])
def test_a_trailing_slash_redirects_onto_a_table_url(app: Any, path: str) -> None:
    """Starlette's slash redirect is kept honest: it may only point at a served URL."""
    status, headers, _ = asgi_request(app, path)
    assert status == 307, path
    target = "/" + headers["location"].split("//", 1)[1].split("/", 1)[1]
    assert target in webui.RESOURCES


def test_a_stray_newline_never_changes_which_file_is_read(app: Any) -> None:
    """A route's regex ends with ``$``, which also matches before a trailing newline.

    That is a Starlette detail, not something this module controls, and it is harmless here
    because the file a route serves comes from its table entry: the extra character cannot
    select a different file.
    """
    status, _, body = asgi_request(app, "/styles.css\n")
    assert status == 200
    assert body == (REPO_ROOT / "ui" / "styles.css").read_bytes()


def test_a_served_path_is_matched_exactly(app: Any) -> None:
    assert webui.path_for("/app.js") is not None
    assert webui.path_for("/App.js") is None
    assert webui.path_for(" /app.js") is None
    assert webui.path_for("/app.js ") is None


def test_a_missing_interface_file_blames_the_install_not_the_request(tmp_path: Path) -> None:
    """The table says a file exists; the disk disagrees. That is a broken install."""
    original = webui.UI_DIR
    webui.UI_DIR = tmp_path
    try:
        response = webui.response_for("/app.js")
    finally:
        webui.UI_DIR = original
    assert response.status_code == 500
    assert json.loads(response.body)["error"]["code"] == "ui_file_missing"


# -------------------------------------------------------------------------- headers


@pytest.mark.parametrize("url_path", sorted(webui.RESOURCES))
def test_the_content_policy_accompanies_every_interface_file(url_path: str) -> None:
    response = webui.response_for(url_path)
    policy = response.headers["content-security-policy"]
    # Everything, including the video the player loads, comes from this origin only.
    for directive in ("default-src 'self'", "script-src 'self'", "media-src 'self'"):
        assert directive in policy
    # The UI is modules and a linked stylesheet, so nothing needs either escape hatch.
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert "cdn" not in policy and "http://" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"


def test_a_non_loopback_host_cannot_load_the_interface(app: Any) -> None:
    """The host guard wraps the page as well as the API (SPEC.md section 10)."""
    status, _, body = asgi_request(app, "/", headers=((b"host", b"evil.example"),))
    assert status == 400
    assert json.loads(body)["error"]["code"] == "host_not_allowed"


def test_an_unexpected_origin_still_builds_the_page(app: Any) -> None:
    """The page and its API calls are same-origin, so CORS never enters the picture."""
    status, headers, _ = asgi_request(
        app, "/api/options", headers=((b"origin", b"http://localhost:8000"),)
    )
    assert status == 200
    assert headers["access-control-allow-origin"] == "http://localhost:8000"


# --------------------------------------------------------------------- html hygiene


def test_the_markup_holds_no_copy_of_its_own(html: str) -> None:
    """Every visible string arrives through i18n.js, so the body starts out text-free."""
    body = html.split("<body>", 1)[1].split("</body>", 1)[0]
    without_comments = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    assert re.sub(r"<[^>]+>", "", without_comments).strip() == ""


def test_the_tab_title_falls_back_to_the_name_from_app_meta(html: str) -> None:
    """SPEC.md section 3 keeps the name in one place; the pre-JS title may not invent one."""
    title = re.search(r"<title>(.*?)</title>", html, flags=re.S)
    assert title is not None
    assert title.group(1).strip() == app_meta.app_name()


@pytest.mark.parametrize("attribute", ["onclick", "onload", "oninput", "onchange", "style="])
def test_the_markup_has_no_inline_handlers_or_styles(html: str, attribute: str) -> None:
    assert attribute not in html


def test_the_page_only_loads_what_this_app_serves(html: str) -> None:
    """No remote content, and no asset that would 404 under the content policy."""
    references = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert references
    assert set(references) <= set(webui.RESOURCES)


def test_the_page_declares_its_language_and_viewport(html: str) -> None:
    assert 'lang="en"' in html
    assert 'name="viewport"' in html
    assert 'charset="utf-8"' in html


def test_the_ui_never_shows_the_developer_short_form(scripts: str, html: str) -> None:
    """ "MMS" is for developer docs only, never user-facing text (SPEC.md section 3)."""
    assert "MMS" not in html
    assert "MMS" not in scripts


def test_the_scripts_never_write_markup(scripts: str) -> None:
    """Text goes in through ``textContent``, so a translation cannot be an injection sink."""
    for sink in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML", "eval("):
        assert sink not in scripts


def test_the_scripts_only_name_endpoints_the_backend_serves(app: Any) -> None:
    """A typo in a URL shows up here instead of as a page that silently never renders."""
    source = "".join(
        (REPO_ROOT / "ui" / name).read_text(encoding="utf-8") for name in ("app.js", "i18n.js")
    )
    literals = set(re.findall(r'"/api/[a-z_/]*"', source))
    assert {
        '"/api/options"',
        '"/api/render"',
        '"/api/jobs/"',
        '"/api/videos/"',
    } == literals
    routes = {getattr(route, "path", "") for route in app.routes}
    assert "/api/options" in routes
    assert "/api/render" in routes
    assert any(path.startswith("/api/jobs/") for path in routes)
    assert any(path.startswith("/api/videos/") for path in routes)


def test_the_scripts_only_fetch_files_the_backend_serves(app: Any) -> None:
    source = "".join(
        (REPO_ROOT / "ui" / name).read_text(encoding="utf-8") for name in ("app.js", "i18n.js")
    )
    literals = set(re.findall(r'"(/(?!api/)[a-z0-9_./-]+)"', source))
    assert {"/locales/en.json", "/locales/id.json"} <= literals
    for literal in literals:
        assert literal in webui.RESOURCES, literal


# -------------------------------------------------------------------------- locale


def test_the_two_locale_files_hold_the_same_keys(locales: dict[str, dict[str, str]]) -> None:
    """SPEC.md section 8: both files always carry both sets of strings."""
    assert set(locales["en"]) == set(locales["id"])


def test_no_locale_value_is_left_empty(locales: dict[str, dict[str, str]]) -> None:
    for name, messages in locales.items():
        for key, value in messages.items():
            assert isinstance(value, str) and value.strip(), f"{name}:{key}"


def test_the_markup_keys_exist_in_both_locales(html: str, locales: Any) -> None:
    keys = set(re.findall(r'data-i18n(?:-title)?="([^"]+)"', html))
    assert keys
    for name, messages in locales.items():
        for key in keys:
            assert key in messages, f"{name} is missing {key}"


def test_the_script_keys_exist_in_both_locales(scripts: str, locales: Any) -> None:
    keys = set(re.findall(r'\bt\("([a-z_.]+)"', scripts))
    keys |= set(re.findall(r"\bt\(`([a-z_.]+)\.\$\{", scripts))
    assert keys
    for name, messages in locales.items():
        for key in keys:
            prefix = key.rstrip(".")
            matches = [k for k in messages if k.startswith(prefix)]
            assert matches, f"{name} is missing {prefix}*"


@pytest.mark.parametrize("animation", ANIMATIONS)
def test_every_advertised_animation_has_a_label(animation: str, locales: Any) -> None:
    for name, messages in locales.items():
        assert f"option.animation.{animation}" in messages, name


@pytest.mark.parametrize("quality", QUALITIES)
def test_every_advertised_quality_has_a_label(quality: str, locales: Any) -> None:
    for name, messages in locales.items():
        assert f"option.quality.{quality}" in messages, name


def test_every_background_type_has_a_label(locales: Any) -> None:
    payload = api_main.options_payload()
    assert len(payload["background_types"]) == 2
    for entry in payload["background_types"]:
        for name, messages in locales.items():
            assert f"option.background.{entry['id']}" in messages, name


def test_the_ui_has_one_control_per_spec_field() -> None:
    """The form is built from the SPEC.md section 5 schema, field for field."""
    source = (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    block = source.split("const CONTROLS = {", 1)[1].split("\n};", 1)[0]
    fields = set(re.findall(r"^  ([a-z_]+): \{", block, flags=re.M))
    expected = set(RenderParams.model_fields) | {"background_type", "background_color"}
    expected.discard("background")
    assert fields == expected


def test_every_control_has_a_label_in_both_locales(locales: Any) -> None:
    source = (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    block = source.split("const CONTROLS = {", 1)[1].split("\n};", 1)[0]
    for name in re.findall(r"^  ([a-z_]+): \{", block, flags=re.M):
        for locale_name, messages in locales.items():
            assert f"field.{name}.label" in messages, f"{locale_name}:{name}"


def test_every_error_the_ui_can_show_has_a_translation(locales: Any) -> None:
    """SPEC.md section 6: the UI shows the translation of a code and falls back to its message.

    The codes named here are the ones this stage can realistically meet, and the whole set is
    finished in ROADMAP 3.5.
    """
    reachable = {
        "unreachable",
        "unexpected",
        "render_failed",
        "render_timeout",
        "render_output_missing",
        "manim_missing",
        "ffmpeg_missing",
        "job_not_found",
        "too_many_lines",
        "total_duration_too_long",
    }
    for name, messages in locales.items():
        assert {f"error.{code}" for code in reachable} <= set(messages), name


def test_the_indonesian_copy_uses_indonesian_words(locales: Any) -> None:
    """A guard against shipping an untranslated English file under the ``id`` name."""
    indonesian = locales["id"]
    assert indonesian["settings.heading"] == "Pengaturan"
    assert indonesian["action.generate"] != locales["en"]["action.generate"]
    assert any("Anda" in value for value in indonesian.values())


# --------------------------------------------------------------- full controls (3.2)


#: The fields SPEC.md section 8 asks for as a slider with a numeric input beside it.
SLIDER_FIELDS = ("animation_duration", "hold_duration", "outline_width")

#: Every number box, including the one that stays a plain field.
NUMBER_FIELDS = ("font_size", *SLIDER_FIELDS)

#: The status line can say any of these, and all of them are copy (SPEC.md section 8).
STATUS_KEYS = (
    "status.ready",
    "status.queued",
    "status.running",
    "status.hd_queued",
    "status.hd_running",
    "status.cached",
    "status.done",
    "status.saving",
    "status.press_play",
    "status.cancelled",
    "status.failed",
)

#: A storage call may only be made where a refusal is caught, since a private window throws.
GUARDED_STORAGE = re.compile(r"try \{\n\s+(?:\w+ = )?localStorage\.(setItem|getItem)\(")


def app_source() -> str:
    return (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")


def controls_block() -> str:
    """The :data:`CONTROLS` table, whose entries stay one object literal per field."""
    return app_source().split("const CONTROLS = {", 1)[1].split("\n};", 1)[0]


def control_body(name: str) -> str:
    match = re.search(rf"^  {name}: \{{([^}}]*)\}}", controls_block(), flags=re.M)
    assert match is not None, name
    return match.group(1)


def test_the_timing_and_outline_fields_are_the_sliders() -> None:
    sliders = {
        name
        for name, body in re.findall(r"^  ([a-z_]+): \{([^}]*)\}", controls_block(), flags=re.M)
        if 'kind: "slider"' in body
    }
    assert sliders == set(SLIDER_FIELDS)


@pytest.mark.parametrize("field", SLIDER_FIELDS)
def test_a_slider_is_a_native_range_boxed_by_the_same_number_field(field: str) -> None:
    """The native range keeps keyboard, touch and screen-reader behavior for free; the number
    box beside it holds the value the form submits and validates."""
    body = control_body(field)
    source = app_source()
    assert 'kind: "slider"' in body
    assert 'slider.type = "range"' in source
    assert 'control.type = "number"' in source
    assert "pair.append(slider, control)" in source


@pytest.mark.parametrize("field", NUMBER_FIELDS)
def test_a_numeric_step_reaches_the_top_of_the_advertised_range(field: str) -> None:
    """A step that does not divide the range leaves the maximum unreachable by dragging or by
    the spinner, and the backend would then reject a value the control allowed."""
    step_match = re.search(r"step: ([0-9.]+)", control_body(field))
    assert step_match is not None, field
    step = float(step_match.group(1))
    limits = api_main.options_payload()["limits"][field]
    span = float(limits["max"]) - float(limits["min"])
    assert span % step == pytest.approx(0.0, abs=1e-9), field


def test_the_slider_takes_its_name_from_the_visible_label() -> None:
    """Two controls, one label: the range points at the text already on screen rather than
    carrying an invented `aria-label` (modern-web-guidance `forms`)."""
    source = app_source()
    assert "label.id = labelId(name);" in source
    assert 'slider.setAttribute("aria-labelledby", labelId(name))' in source
    assert '"aria-label"' not in source


def test_a_slider_with_a_unit_is_spoken_through_intl() -> None:
    """`aria-valuetext` is the range's own slot for a formatted value. The words come from
    `Intl`, which is what says "1 second" and "2,5 detik" without a locale key per plural
    (SPEC.md section 8); a field with no unit to name announces its plain number instead."""
    source = app_source()
    assert '"aria-valuetext"' in source
    assert "const UNIT_FORMATTERS = { seconds };" in source
    assert 'unit: "seconds"' in controls_block()
    assert "unit: null" in controls_block()


def test_durations_are_said_with_the_locale_unit(scripts: str) -> None:
    assert 'unit: "second"' in scripts
    assert "export function seconds(value)" in scripts


@pytest.mark.parametrize("action", ["generate", "hd"])
def test_both_render_buttons_validate_before_requesting(action: str) -> None:
    assert f'requestRender("{action}")' in app_source()


def test_the_hd_button_fixes_the_quality_and_asks_for_an_attachment() -> None:
    source = app_source()
    assert 'const HD_QUALITY = "hd";' in source
    assert "quality: HD_QUALITY" in source
    assert 'const DOWNLOAD_QUERY = "?download=1";' in source
    assert "link.href = source + DOWNLOAD_QUERY;" in source


def test_the_hd_quality_is_one_the_backend_offers() -> None:
    ids = {entry["id"] for entry in api_main.options_payload()["qualities"]}
    assert "hd" in ids


def test_only_the_apps_own_video_path_becomes_a_source_or_a_link() -> None:
    """Both the player and the download get their URL from a response, so neither accepts
    anything that does not start at the endpoint that serves cached videos."""
    source = app_source()
    assert "url.startsWith(ENDPOINTS.video)" in source
    assert "view.player.src = source;" in source
    assert "link.href = source + DOWNLOAD_QUERY;" in source


def test_the_running_state_disables_both_actions_and_offers_cancel() -> None:
    source = app_source()
    assert "view.generate.disabled = busy;" in source
    assert "view.download.disabled = busy;" in source
    assert "view.cancel.hidden = !busy;" in source
    assert "view.busy.hidden = !busy;" in source


def test_cancel_retires_the_wait_instead_of_submitting_the_form(html: str) -> None:
    """The button is a `type="button"`, and the wait checks the token it was started with, so a
    cancelled render cannot write over what the user did next."""
    cancel = re.search(r'<button[^>]*id="cancel"[^>]*>', html)
    assert cancel is not None
    assert 'type="button"' in cancel.group(0)
    assert "hidden" in cancel.group(0)
    source = app_source()
    assert "view.cancel.addEventListener(" in source
    assert "runToken += 1;" in source
    assert "if (token !== runToken) {" in source


@pytest.mark.parametrize("tag", ["generate", "download-hd", "cancel", "retry"])
def test_every_button_is_empty_and_named_by_a_locale_key(html: str, tag: str) -> None:
    """No copy in the markup, and no button that defaults to submitting the form."""
    button = re.search(rf'<button[^>]*id="{tag}"[^>]*>\s*</button>', html)
    assert button is not None, tag
    markup = button.group(0)
    assert 'data-i18n="action.' in markup
    if 'type="submit"' not in markup:
        assert 'type="button"' in markup


def test_the_settings_are_remembered_under_the_name_from_app_meta() -> None:
    """SPEC.md section 8 asks for the last settings; the key keeps the app's own slug so the
    name still has one owner (SPEC.md section 3)."""
    source = app_source()
    assert "${options.app.slug}" in source
    assert "saveSettings();" in source
    assert "restoreSettings();" in source


def test_local_storage_is_only_touched_where_a_refusal_is_caught() -> None:
    assert len(GUARDED_STORAGE.findall(app_source())) == 2


def test_a_remembered_value_is_used_only_if_the_options_still_allow_it() -> None:
    """Local storage is not trusted: an older version, an edited file, or a limit that has
    since moved must not push an unusable value into a render request."""
    source = app_source()
    assert "allowedValue(name, value)" in source
    assert "stored.background?.type" in source
    assert "options.hex_color_pattern" in source
    assert 'value.split("\\n").length <= limits.max_lines' in source


def test_every_hint_key_in_the_controls_is_translated(locales: Any) -> None:
    keys = set(re.findall(r'hint: "([a-z_.]+)"', controls_block()))
    assert {"hint.text", "hint.seconds", "hint.points", "hint.number"} == keys
    for name, messages in locales.items():
        for key in keys:
            assert key in messages, f"{name} is missing {key}"


@pytest.mark.parametrize("key", STATUS_KEYS)
def test_every_status_the_interface_can_show_has_a_translation(key: str, locales: Any) -> None:
    for name, messages in locales.items():
        assert key in messages, name


@pytest.mark.parametrize("key", ["action.download_hd", "action.cancel", "action.retry"])
def test_the_new_actions_are_worded_differently_in_indonesian(key: str, locales: Any) -> None:
    """A guard against shipping English copy under the Indonesian key for a new button."""
    assert locales["id"][key] != locales["en"][key]


# ------------------------------------------------------------------ served contract


def test_the_served_page_is_the_one_the_scripts_and_styles_are_written_for(app: Any) -> None:
    """The bytes the browser receives are the bytes in the repository, not a stale copy."""
    for url_path, (relative, _) in webui.RESOURCES.items():
        status, _, body = asgi_request(app, url_path)
        assert status == 200, url_path
        assert body == (REPO_ROOT / "ui" / relative).read_bytes()


def test_the_form_posts_to_the_render_endpoint(app: Any, html: str) -> None:
    """Without JavaScript the page still asks the endpoint that exists, not a made-up one."""
    action = re.search(r'<form[^>]*action="([^"]+)"', html)
    method = re.search(r'<form[^>]*method="([^"]+)"', html)
    assert action is not None and method is not None
    assert action.group(1) == "/api/render"
    assert method.group(1).lower() == "post"
    status, _, _ = asgi_request(app, "/api/render", method="POST")
    assert status == 422  # an empty POST body is not JSON, and says so with a stable code


def test_font_names_are_never_treated_as_copy(locales: Any) -> None:
    """SPEC.md section 9: the font list is data from the API, so no locale file repeats it."""
    for name, messages in locales.items():
        assert not any(font in key for key in messages for font in FONTS), name
        assert not any(value in FONTS for value in messages.values()), name


def test_the_player_is_sized_from_the_quality_presets() -> None:
    """The renderer's presets, not a literal in the markup, decide the player's size."""
    source = (REPO_ROOT / "ui" / "app.js").read_text(encoding="utf-8")
    assert "player.width = quality.width" in source
    assert "player.height = quality.height" in source
    payload = api_main.options_payload()
    presets = {entry["id"]: (entry["width"], entry["height"]) for entry in payload["qualities"]}
    assert presets["preview"] == (854, 480)
