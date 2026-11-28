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
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from backend.app import app_meta, webui
from backend.app import main as api_main
from backend.app.models import ANIMATIONS, FONTS, NUMERIC_RANGES, QUALITIES, RenderParams

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


def test_every_error_the_ui_can_show_has_a_translation() -> None:
    """SPEC.md section 6: the UI shows the translation of a code and falls back to its message.

    ROADMAP 3.5 closed the set: the list lives in the test below, which reads the codes out of
    the backend itself rather than remembering them here.
    """
    locales = load_locales()
    for name, messages in locales.items():
        assert {f"error.{code}" for code in backend_error_codes()} <= set(messages), name


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
    assert "${options?.app.slug" in source
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


# --------------------------------------------------------------- accessibility (3.3)


#: Foreground and background that carry text the user has to read, in either scheme.
#: `accent-ink` on `accent` and on `accent-hover` is Generate, at rest and under the pointer;
#: `ink` on `raise` is the field label when the control it names is hovered; `danger` on
#: `danger-bg` is the icon beside an error.
TEXT_PAIRS = (
    ("ink", "canvas"),
    ("ink", "panel"),
    ("ink", "raise"),
    ("ink-muted", "canvas"),
    ("ink-muted", "panel"),
    ("accent", "canvas"),
    ("accent", "panel"),
    ("accent-ink", "accent"),
    ("accent-ink", "accent-hover"),
    ("ink", "danger-bg"),
    ("danger", "danger-bg"),
)


def style_source() -> str:
    return (REPO_ROOT / "ui" / "styles.css").read_text(encoding="utf-8")


def scheme_tokens(*, dark: bool) -> dict[str, str]:
    """The color custom properties of one scheme, read from the stylesheet itself.

    Enforcing the ratio against the file rather than a list copied into the test is what makes
    this useful after the design pass: a token changed in ``styles.css`` is measured again.
    """
    css = style_source()
    block = (
        css.split("@media (prefers-color-scheme: dark)", 1)[1]
        if dark
        else css.split(":root {", 1)[1]
    )
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", block.split("}", 1)[0]))


def _channel(hex_pair: str) -> float:
    value = int(hex_pair, 16) / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def contrast(first: str, second: str) -> float:
    """The WCAG 2.x contrast ratio of two ``#RRGGBB`` colors."""
    luminances = []
    for color in (first, second):
        red, green, blue = (_channel(color[start : start + 2]) for start in (1, 3, 5))
        luminances.append(0.2126 * red + 0.7152 * green + 0.0722 * blue)
    lighter, darker = max(luminances), min(luminances)
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("front,back", TEXT_PAIRS)
def test_every_text_color_pair_passes_wcag_aa(front: str, back: str, dark: bool) -> None:
    """1.4.3 asks 4.5:1 for text this small, and every pair below carries real words."""
    tokens = scheme_tokens(dark=dark)
    ratio = contrast(tokens[front], tokens[back])
    assert ratio >= 4.5, f"{front} on {back} is {ratio:.2f}:1"


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_the_focus_ring_is_visible_against_both_backgrounds(dark: bool) -> None:
    """The outline is the only thing that says where the keyboard is, so it is a non-text
    indicator and needs 3:1 against what it sits on (WCAG 1.4.11)."""
    tokens = scheme_tokens(dark=dark)
    for background in ("canvas", "panel"):
        ratio = contrast(tokens["accent"], tokens[background])
        assert ratio >= 3.0, f"against {background}: {ratio:.2f}:1"


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_the_edge_of_an_operable_control_is_visible(dark: bool) -> None:
    """`--boundary` draws the edge of every field, button and the player. WCAG 1.4.11 asks 3:1
    for the boundary of a user interface component, so the token is measured rather than
    eyeballed, and `--line` is left for the card edges that hold no control. `raise` counts too:
    a hovered field keeps its edge, and the dark scheme needed a lighter boundary for that."""
    tokens = scheme_tokens(dark=dark)
    for background in ("canvas", "panel", "raise"):
        ratio = contrast(tokens["boundary"], tokens[background])
        assert ratio >= 3.0, f"against {background}: {ratio:.2f}:1"


def test_the_keyboard_position_is_always_shown() -> None:
    """`:focus-visible` draws the ring only for keyboard interaction, and nothing in the
    stylesheet is allowed to take it away again."""
    css = style_source()
    rule = re.search(r":focus-visible \{([^}]*)\}", css)
    assert rule is not None
    width = re.search(r"outline: (\d+)px solid var\(--accent\)", rule.group(1))
    assert width is not None and int(width.group(1)) >= 2
    assert "outline-offset:" in rule.group(1)
    assert not re.search(r"outline:\s*(?:none|0)\s*[;!]", css)


def test_reduced_motion_stops_the_movement_the_page_adds() -> None:
    """Not only the durations: a repeating animation has to stop repeating, and scrolling has
    to stop sliding (modern-web-guidance `prefers-reduced-motion`)."""
    css = style_source()
    block = re.search(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, flags=re.S)
    assert block is not None
    for declaration in (
        "transition-duration",
        "animation-duration",
        "animation-iteration-count",
        "scroll-behavior",
    ):
        assert declaration in block.group(1), declaration


def test_a_finished_video_does_not_start_moving_by_itself_when_motion_is_reduced() -> None:
    """The stylesheet cannot reach the player, and a clip filling the pane is the largest
    movement on the screen, so the same query decides whether it autoplays (SPEC.md section 8)."""
    source = app_source()
    assert 'const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";' in source
    shown = source.split("async function showVideo(url) {", 1)[1].split("\n}", 1)[0]
    assert "window.matchMedia(REDUCED_MOTION).matches" in shown
    assert 'setStatus("status.press_play");' in shown
    assert "await view.player.play();" in shown  # the ordinary path still plays inline


def test_focus_stays_in_the_actions_row_while_a_job_runs() -> None:
    """SPEC.md section 8 disables Generate during a render, and a disabled button drops focus to
    the top of the page. The row hands the caret to the control that replaced it and takes it
    back when it is idle, but never from somewhere the user has already moved to."""
    source = app_source()
    busy = source.split("function setBusy(busy) {", 1)[1].split("\n}", 1)[0]
    assert "const next = busy ? view.cancel : view.generate;" in busy
    assert "next.focus();" in busy
    assert "view.settings.contains(active)" in busy


def test_the_page_follows_the_system_color_scheme() -> None:
    """SPEC.md section 8: respect `prefers-color-scheme`. Both schemes must define the same
    tokens, or one of them renders with a property left over from the other."""
    css = style_source()
    assert "color-scheme: light dark;" in css
    assert len(scheme_tokens(dark=False)) == len(scheme_tokens(dark=True)) >= len(TEXT_PAIRS)


@pytest.mark.parametrize(("element", "live"), [("job-status", "status"), ("job-error", "alert")])
def test_the_line_that_changes_says_that_it_changes(element: str, live: str, html: str) -> None:
    """The status and the error are the only feedback for a job a user cannot see running."""
    tag = re.search(rf"<[a-z]+[^>]*id=\"{element}\"[^>]*>", html, flags=re.S)
    assert tag is not None, element
    assert f'role="{live}"' in tag.group(0)


@pytest.mark.parametrize(
    ("element", "heading"), [("player", "output-heading"), ("busy", "job-status")]
)
def test_a_control_with_no_text_of_its_own_is_named_by_one_that_has(
    element: str, heading: str, html: str
) -> None:
    """A video and a progress bar hold no words, so their accessible name comes from the text
    already on the screen beside them."""
    tag = re.search(rf"<(?:video|progress)[^>]*id=\"{element}\"[^>]*>", html, flags=re.S)
    assert tag is not None, element
    assert f'aria-labelledby="{heading}"' in tag.group(0)


def test_every_built_control_is_labelled_by_a_real_label_element() -> None:
    """`<label for>` rather than an invented `aria-label`, so the name a screen reader says is
    the name the user can see and click (WCAG 1.3.1, 3.3.2)."""
    source = app_source()
    assert "label.htmlFor = fieldId(name);" in source
    assert "wrap.append(label)" in source


# ------------------------------------------------------------------ design pass (3.4)


#: The properties whose values are rhythm rather than type: these are the ones that have to sit
#: on the 4px scale (unslop pass 1).
SPACING_PROPERTIES = (
    "margin",
    "margin-block-end",
    "padding",
    "gap",
    "block-size",
    "inline-size",
    "min-block-size",
)


def _spacing_values(css: str) -> list[tuple[str, str]]:
    """Every ``(property, value)`` pair in the file that decides spacing."""
    found = []
    for name, value in re.findall(
        rf"^\s*({'|'.join(SPACING_PROPERTIES)}):\s*([^;]+);", css, flags=re.M
    ):
        found.append((name, value.strip()))
    return found


def test_the_rhythm_is_on_one_scale() -> None:
    """4px steps, so equal things have equal sizes and nothing is nudged by a stray 4.8px. The
    three gap tokens are checked too, because most declarations read their value from them."""
    css = style_source()
    checked = 0
    for name, value in _spacing_values(css):
        for rem in re.findall(r"([\d.]+)rem", value):
            steps = float(rem) * 16 / 4
            assert steps == int(steps), f"{name}: {value} is not on the 4px scale"
            checked += 1
        for pixels in re.findall(r"([\d.]+)px", value):
            assert float(pixels) % 4 == 0, f"{name}: {value} is not on the 4px scale"
            checked += 1
    for name, rem in re.findall(r"--gap-(\w+):\s*([\d.]+)rem;", css):
        steps = float(rem) * 16 / 4
        assert steps == int(steps), f"--gap-{name} is not on the 4px scale"
        checked += 1
    assert checked > 20, f"only {checked} spacing values were measured"


def test_the_section_labels_are_words_and_not_shouting() -> None:
    """Sentence case, normal tracking: the words are already short and a screen reader still has
    to read them out (unslop pass 5, WCAG 3.3.2 aside)."""
    css = style_source()
    assert "text-transform" not in css
    assert "letter-spacing" not in css


def test_the_hierarchy_does_not_come_from_bold() -> None:
    """Three weights would be a decision; 550, 650 and 700 were three accidents."""
    weights = {int(value) for value in re.findall(r"font-weight:\s*(\d+)", style_source())}
    assert weights and all(400 <= weight <= 600 for weight in weights), weights


def test_the_error_mark_is_an_icon_and_not_a_letter(html: str) -> None:
    """The "!" the stylesheet used to write is decoration no assistive technology can name, and
    CSS text is not a translation (SPEC.md section 8)."""
    css = style_source()
    assert 'content: "' not in css
    assert "content:'" not in css
    icon = re.search(r"<svg[^>]*class=.icon.[^>]*>", html, flags=re.S)
    assert icon is not None
    assert 'aria-hidden="true"' in icon.group(0)
    assert 'stroke-width="1.5"' in icon.group(0)


def test_the_pointer_gets_an_answer_and_the_keyboard_keeps_its_own() -> None:
    """A hover and a press state exist, and the only motion the page adds is a color or surface
    step: no `transition: all`, no animated layout property."""
    css = style_source()
    assert re.search(r":hover \{|,?\s*:hover", css)
    assert ":active" in css
    for declaration in re.findall(r"transition:\s*([^;]+);", css):
        assert "all" not in declaration, declaration


def test_one_accent_marks_one_action() -> None:
    """Generate is the only filled button on the screen, so the second action of the form has to
    live in the neutral ramp (unslop pass 2)."""
    css = style_source()
    secondary = css.split("button.secondary {", 1)[1].split("}", 1)[0]
    assert "var(--accent)" not in secondary


def test_the_frame_shows_the_values_the_render_will_get(html: str) -> None:
    """The preview pane is the video's own canvas: the two colors and the shape that go into the
    request, not a picture of an idea of them, and not a sentence about a place."""
    source = app_source()
    css = style_source()
    for hook in ('"--preview-background"', '"--preview-color"', '"--preview-ratio"'):
        assert hook in source, hook
    assert 'view.frameText.textContent = valueOf("text");' in source
    for use in (
        "background: var(--preview-background)",
        "color: var(--preview-color)",
        "aspect-ratio: var(--preview-ratio)",
    ):
        assert use in css, use
    frame = re.search(r'<div class="frame"[^>]*>', html)
    assert frame is not None and 'aria-hidden="true"' in frame.group(0)
    # The text inside it is written by the script, so the markup still carries no copy.
    assert 'id="preview-frame-text"></p>' in html


def test_the_frame_text_scales_with_the_frame_and_stays_clamped() -> None:
    """The line grows the way the render grows it, and the clamp keeps three lines of the
    longest text the form allows inside the box (WCAG 1.4.10, 1.4.4)."""
    css = style_source()
    block = css.split(".frame-text {", 1)[1].split("}", 1)[0]
    clamp = re.search(r"font-size: clamp\(([^)]*)\)", block)
    assert clamp is not None
    low, high = (
        float(re.findall(r"[\d.]+(?=rem)", clamp.group(1))[0]),
        float(re.findall(r"[\d.]+(?=rem)", clamp.group(1))[-1]),
    )
    assert high / low <= 2.5, "the zoom ceiling in the modern-web-guidance typography guide"
    assert "container-type: inline-size" in css
    assert "-webkit-line-clamp: 3" in block
    # The line breaks the renderer receives are the breaks the slate shows.
    assert "white-space: pre-line" in block


def test_a_short_string_does_not_end_in_a_period(locales: Any) -> None:
    """Labels, buttons and captions lose the full stop; a string that is two sentences keeps them,
    because then it is a paragraph and not a label (unslop pass 4)."""
    for name, messages in locales.items():
        for key, value in messages.items():
            if value.endswith(".") and ". " not in value.removesuffix("."):
                raise AssertionError(f"{name}:{key} = {value!r}")
            assert "!" not in value, f"{name}:{key}"


def test_the_empty_state_holds_no_sentence_about_the_place(locales: Any) -> None:
    """The frame replaced `Your video appears here once it has rendered.`: the slate says it, and
    the line under it carries the real numbers instead."""
    for name, messages in locales.items():
        assert "player.empty" not in messages, name
    source = app_source()
    assert "playerEmpty" not in source
    assert "view.frame.hidden = true;" in source


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


# ------------------------------------------------------------------ localization (3.5)


#: Backend files that put a code into a SPEC.md section 6 error object. The set is read out of
#: these sources rather than copied into a test, so a new code arrives in the locale files
#: because the suite noticed, not because the next person remembered (ROADMAP 3.5).
ERROR_SOURCES = (
    Path("backend/app/models.py"),
    Path("backend/app/main.py"),
    Path("backend/app/cache.py"),
    Path("backend/app/videos.py"),
    Path("backend/app/render.py"),
    Path("backend/app/jobs.py"),
    Path("backend/app/security.py"),
    Path("backend/app/webui.py"),
)

#: The codes `app.js` invents for a failure that never reached the backend, and the fallback key
#: `errorText` reads when no translation exists; neither is a code the backend can raise.
UI_ONLY_CODES = frozenset({"unreachable", "unexpected", "http"})

#: The fields of the SPEC.md section 5 schema, spelled as the UI names them: the two background
#: controls the form shows for the one `background` object.
TRANSLATED_FIELDS = (
    "text",
    "animation",
    "animation_duration",
    "hold_duration",
    "font",
    "font_size",
    "text_color",
    "outline_color",
    "outline_width",
    "background",
    "background_type",
    "quality",
)

#: The fields whose value must land inside a range, so only these can answer `_out_of_range`.
NUMERIC_RANGE_FIELDS = tuple(NUMERIC_RANGES)

#: Codes :func:`backend.app.models._derived_error` builds from a field name plus a suffix, so no
#: source file holds them as literals. They are spelled out here instead, and
#: `test_the_translated_field_names_are_the_schema` proves each name is still a real field: rename
#: one and the stale code surfaces there rather than as an untranslated sentence on screen. Only a
#: range-checked field can arrive with `_out_of_range`; `_invalid` is derived from any field the
#: input gets wrong.
DERIVED_CODES = frozenset(
    f"{field}{suffix}"
    for field in TRANSLATED_FIELDS
    for suffix in (
        ("_invalid", "_out_of_range") if field in NUMERIC_RANGE_FIELDS else ("_invalid",)
    )
)


def load_locales() -> dict[str, dict[str, str]]:
    return {
        name: json.loads((REPO_ROOT / "ui" / "locales" / f"{name}.json").read_text("utf-8"))
        for name in ("en", "id")
    }


def backend_error_codes() -> set[str]:
    """Every code literal the backend can answer with."""
    custom = re.search(
        r"_CUSTOM_CODES = frozenset\(\s*\n?\s*\{(.*?)\}\s*\)",
        (REPO_ROOT / "backend" / "app" / "models.py").read_text("utf-8"),
        flags=re.S,
    )
    assert custom is not None, "the custom code list moved in models.py"
    codes = set(re.findall(r'"([a-z_]+)"', custom.group(1)))
    patterns = (
        r'\bcode="([a-z_]+)"',
        r'"code": "([a-z_]+)"',
        r"error_object\(\s*\"([a-z_]+)\"",
        r"_error_response\(\s*\d+,\s*\"([a-z_]+)\"",
        r"_error\(\s*\d+,\s*\"([a-z_]+)\"",
        r"CacheError\(\s*\"([a-z_]+)\"",
        r"VideoError\(\s*\"([a-z_]+)\"",
        r"RenderError\(\s*\"([a-z_]+)\"",
        r"RenderParamsError\(\s*\"([a-z_]+)\"",
        r"_fail\(\s*job,\s*\"([a-z_]+)\"",
        r"_failure\(\s*destination_dir,\s*stem,\s*outcome\.output,\s*\"([a-z_]+)\"",
    )
    for relative in ERROR_SOURCES:
        source = (REPO_ROOT / relative).read_text("utf-8")
        for pattern in patterns:
            codes |= set(re.findall(pattern, source, flags=re.S))
    return codes


def run_i18n_in_node(tmp_path: Path, body: str) -> str:
    """Run a snippet against the real `ui/i18n.js` in the engine the page's module system runs in.

    The bytes are the shipped file's, copied verbatim under a `.mjs` name: that is how Node is
    told it is a module, the same job the page does with `<script type="module">`. Without the
    extension Node reads any `package.json` above the file, and a repository root that happens to
    carry one would silently turn the check into a CommonJS load. The snippet imports its own
    names, so nothing collides.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed, so the language rules cannot be executed here")
    shipped = REPO_ROOT / "ui" / "i18n.js"
    copy = tmp_path / "i18n.mjs"
    copy.write_bytes(shipped.read_bytes())
    module = "file:///" + copy.as_posix()
    names = (
        "DEFAULT_LOCALE, LOCALE_FILES, NATIVE_NAMES, errorText, loadLocale, locale, "
        "lookup, pickLocale, systemLanguages, t"
    )
    imports = (
        "import assert from 'node:assert';\n" + f"const {{ {names} }} = await import({module!r});\n"
    )
    script = tmp_path / "check.mjs"
    script.write_text(imports + body, encoding="utf-8")
    environment = {**os.environ, "MTS_UI_DIR": (REPO_ROOT / "ui").as_posix()}
    result = subprocess.run(
        [node, str(script)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, f"node said: {result.stderr}"
    return result.stdout


#: Node 21 and later ship a read-only `navigator` of their own, so the stubs are defined over it
#: rather than assigned; a browser has no such property and the module only reads what is there.
#: The fetch stub is the file:// stand-in for the origin FastAPI serves the page and the locale
#: files from, so a snippet loads the same bytes the browser would.
I18N_STUB = """
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const UI = process.env.MTS_UI_DIR;
const slot = (value) => ({ value, writable: true, configurable: true });
const stub = (values) => Object.defineProperties(
  globalThis,
  Object.fromEntries(Object.entries(values).map(([name, value]) => [name, slot(value)]))
);
const send = (path) => ({ ok: true, json: async () => JSON.parse(readFileSync(path, 'utf8')) });
stub({
  window: {},
  document: { documentElement: {} },
  navigator: { languages: ["en-US"] },
  fetch: async (url) => {
    const file = url.replace('/locales/', '');
    const path = `${UI}/locales/${file}`;
    if (!['en.json', 'id.json'].includes(file)) {
      throw new TypeError(`not served: ${url}`);
    }
    return send(path);
  },
});
"""

#: The language rules of SPEC.md section 8, executed rather than asserted about.
I18N_LANGUAGE_CHECKS = (
    I18N_STUB
    + """
const idTags = ["id-ID", "id", "id-ID-u-ca-gregory-nu-latn", "in-ID", "zh-CN", "en-US"];
assert.deepEqual(
  idTags.map((tag) => pickLocale([tag])),
  ["id", "id", "id", "id", "en", "en"],
  "the language subtag decides, not the whole tag"
);
assert.deepEqual(pickLocale(["zh-CN", "id-ID"]), "id", "any tag in the list may carry it");
const nothing = "a machine that reports nothing is not Indonesian";
assert.equal(pickLocale([]), DEFAULT_LOCALE, nothing);
const letters = "a tag must be the language, not start with its letters";
assert.equal(pickLocale(["idiomatic"]), "en", letters);
const cased = "the check does not depend on the browser's lowercasing";
assert.equal(pickLocale(["EN"]), "en", cased);
stub({ window: { mtsSystemLanguages: ["id-ID"] } });
stub({ navigator: { languages: ["en-US"], language: "en-US" } });
assert.deepEqual(systemLanguages(), ["id-ID"], "the Electron bridge wins when it is there");
stub({ window: {} });
assert.deepEqual(systemLanguages(), ["en-US"], "navigator.languages is the browser path");
stub({ navigator: { languages: [], language: "de-DE" } });
assert.deepEqual(systemLanguages(), ["de-DE"], "navigator.language is the older fallback");
stub({ navigator: {} });
assert.deepEqual(systemLanguages(), [], "a host that reports nothing is handled");
assert.equal(LOCALE_FILES.id, "/locales/id.json");
assert.deepEqual(Object.keys(NATIVE_NAMES), ["en", "id"]);
stub({ navigator: { languages: ["en-US"] } });
await loadLocale("en");
assert.equal(pickLocale(systemLanguages()), "en");
assert.equal(document.documentElement.lang, "en");
await loadLocale("id");
assert.equal(locale(), "id", "the Indonesian file loads through the same path the page uses");
assert.equal(document.documentElement.lang, "id");
assert.equal(t("settings.heading"), "Pengaturan");
assert.equal(lookup("settings.heading"), "Pengaturan");
await loadLocale("en");
assert.equal(lookup("settings.heading"), "Settings", "a switch back reloads the other file");
console.log("language rules ok");
"""
)


def test_the_language_rules_run_in_the_browsers_own_engine(tmp_path: Path) -> None:
    """Node executes the same ES module the page imports, over stubbed `navigator` objects."""
    out = run_i18n_in_node(tmp_path, I18N_LANGUAGE_CHECKS)
    assert out.strip().endswith("language rules ok")


#: The three rungs of SPEC.md section 6: the code's translation, its numbers, then the English
#: message the backend sent.
I18N_ERROR_CHECKS = (
    I18N_STUB
    + """
await loadLocale("en");
const cases = [
  [errorText({ code: "text_too_long", params: { max: 100, got: 137 } }), "137 characters"],
  [errorText({ code: "http_418", params: { status: 418 } }), "HTTP 418"],
  [errorText({ code: "never_seen_in_any_locale", params: {}, message: "the raw fallback" }),
   "the raw fallback"],
];
const failed = cases.filter(([text, expected]) => !text.includes(expected));
assert.deepEqual(failed, [], JSON.stringify(cases));
const indonesian = JSON.parse(readFileSync(`${UI}/locales/id.json`, 'utf8'));
await loadLocale("id");
assert.equal(locale(), "id");
assert.equal(errorText({ code: "job_not_found", params: {} }), indonesian["error.job_not_found"]);
const english = "instead of a result";
const status = errorText({ code: "http_503", params: { status: 503 } });
assert.ok(!status.includes(english), status);
assert.ok(status.includes("Kodanya HTTP 503"), status);
const grouped = errorText({ code: "text_too_long", params: { max: 100, got: 1234 } });
assert.ok(grouped.includes("1.234"), grouped);
console.log("error copy ok");
"""
)


def test_the_error_copy_prefers_the_code_and_keeps_the_fallback(tmp_path: Path) -> None:
    """The translation of a `code` wins; an unknown code falls back to the English `message`."""
    out = run_i18n_in_node(tmp_path, I18N_ERROR_CHECKS)
    assert out.strip().endswith("error copy ok")


def test_every_backend_error_code_is_translated_in_both_locales(
    locales: dict[str, dict[str, str]],
) -> None:
    """ROADMAP 3.5: the error codes are translated, all of them, in both languages."""
    codes = backend_error_codes() | DERIVED_CODES
    assert len(codes) >= 30, f"only {len(codes)} codes were found; the extraction has rotted"
    for name, messages in locales.items():
        missing = sorted(code for code in codes if f"error.{code}" not in messages)
        assert not missing, f"{name} is missing {missing}"


def test_the_translated_field_names_are_the_schema() -> None:
    """A derived code is only worth translating while the field it names exists.

    The UI's two background controls edit the one `background` object, so `background_type` is the
    exception the schema does not carry; every other name has to be a real field.
    """
    fields = set(RenderParams.model_fields) | {"background_type"}
    assert set(TRANSLATED_FIELDS) <= fields, sorted(set(TRANSLATED_FIELDS) - fields)


def test_no_locale_translates_a_code_that_no_longer_exists(
    locales: dict[str, dict[str, str]],
) -> None:
    """The other direction: retired copy reads as live wording and hides a removed code."""
    for name, messages in locales.items():
        error_keys = {
            key.removeprefix("error.")
            for key in messages
            if key.startswith("error.") and key != "error.unexpected"
        }
        stray = sorted(error_keys - backend_error_codes() - DERIVED_CODES - UI_ONLY_CODES)
        assert not stray, f"{name} translates codes nothing raises: {stray}"


def test_the_parity_rule_fails_for_a_key_present_in_only_one_file() -> None:
    """The acceptance ROADMAP 3.5 states, demonstrated: one key in one file, and the rule is red.

    `test_the_two_locale_files_hold_the_same_keys` is the rule; this shows the rule bites, by
    re-running its assertion on a copy with one key removed.
    """
    messages = load_locales()
    for dropped in ("error.text_too_long", "language.label"):
        assert dropped in messages["en"], dropped
        short = {key: value for key, value in messages["en"].items() if key != dropped}
        with pytest.raises(AssertionError):
            assert set(short) == set(messages["id"])


def test_both_locale_files_use_the_same_placeholders(locales: dict[str, dict[str, str]]) -> None:
    """A `{max}` in one file only shows a literal brace to the other language."""
    holder = re.compile(r"\{(\w+)\}")
    for key, english in locales["en"].items():
        assert key in locales["id"], key
        assert set(holder.findall(english)) == set(holder.findall(locales["id"][key])), key


@pytest.mark.parametrize(
    "key",
    ["language.label", "error.text_too_long", "error.params_not_json", "error.http"],
)
def test_the_new_copy_is_actually_indonesian(key: str, locales: dict[str, dict[str, str]]) -> None:
    """A guard for the strings this task adds, so `id` is not the English file under a new name."""
    assert locales["id"][key] != locales["en"][key], key


def test_the_indonesian_file_keeps_indonesian_spelling(locales: dict[str, dict[str, str]]) -> None:
    """Everyday words are translated; "render" stays the borrowed verb of the trade."""
    english_words = ("color", "favorite", "your video", "settings", "cancel", "language")
    for key, value in locales["id"].items():
        for word in english_words:
            assert word.lower() not in value.lower(), f"id:{key} = {value!r}"


def test_the_language_picker_is_an_option_list_with_a_label(html: str) -> None:
    """SPEC.md section 8: a switcher the keyboard reaches and a screen reader can name."""
    select = re.search(r'<select[^>]*id="language"[^>]*></select>', html)
    assert select is not None
    assert 'data-i18n-title="language.title"' in select.group(0)
    label = re.search(r'<label[^>]*for="language"[^>]*data-i18n="language\.label"', html)
    assert label is not None


def test_the_picker_names_each_language_in_its_own_words(
    locales: dict[str, dict[str, str]],
) -> None:
    """A picker whose options are translated cannot be read by the person switching to read it, so
    the names live in `i18n.js` and no locale file carries them (SPEC.md section 8)."""
    source = (REPO_ROOT / "ui" / "i18n.js").read_text("utf-8")
    assert "export const NATIVE_NAMES = {" in source
    assert 'id: "Bahasa Indonesia"' in source
    for name, messages in locales.items():
        for key, value in messages.items():
            assert "Bahasa Indonesia" not in value, f"{name}:{key}"
            assert value.strip() != "English", f"{name}:{key}"


def test_the_chosen_language_is_remembered_and_shown_as_the_truth() -> None:
    """The override survives a reload, and the select reports the locale on screen, which may have
    been decided by the system rather than by the user."""
    source = app_source()
    assert 'const LOCALE_SUFFIX = "locale.v1";' in source
    assert "writeStoredString(localeKey(), requested);" in source
    assert "const stored = readStoredString(localeKey());" in source
    assert "select.value = locale();" in source


def test_switching_language_rebuilds_the_form_and_keeps_the_values() -> None:
    """A label, an option and an `Intl` hint are all made when the form is built, so the switch
    rebuilds the form and carries the user's values across it."""
    body = app_source().split("async function switchLocale(requested) {", 1)[1].split("\n}", 1)[0]
    for step in (
        "await loadLocale(requested);",
        "applyText(document);",
        "applyDocumentTitle(options.app.name);",
        "buildForm();",
        "applyFormValues(values);",
        "syncSliders();",
        "paintPreview();",
        "sizePlayer();",
    ):
        assert step in body, step
    assert "const values = currentFormValues();" in body


def test_the_lines_that_are_already_visible_get_said_again() -> None:
    """The status under a running render and an error on screen must not stay in the old
    language, and a new error replaces the remembered one."""
    source = app_source()
    assert "lastStatusKey = key;" in source
    assert "setStatus(lastStatusKey);" in source
    assert "view.errorText.textContent = errorText(lastError);" in source
    assert "lastError = api;" in source
    hide = source.split("function hideError() {", 1)[1].split("\n}", 1)[0]
    assert "lastError = null;" in hide


def test_an_http_status_without_an_error_object_shares_one_line() -> None:
    """A response with no JSON error body has no stable code, so one translated line carries the
    status number instead of inventing a key per code."""
    source = (REPO_ROOT / "ui" / "i18n.js").read_text("utf-8")
    assert 'startsWith("http")' in source
    assert 'lookup("error.http")' in source
    assert "code: `http_${response.status}`" in app_source()


def test_the_language_row_cannot_clip_indonesian() -> None:
    """SPEC.md section 8 asks the layout to tolerate the longer language. The picker is a shrinkable
    flex item and nothing in its row fixes a width or hides what overflows, so "Basa Inggris" and
    the Indonesian label both fit the way the English copy does."""
    css = style_source()
    header = css.split(".masthead {", 1)[1].split("}", 1)[0]
    row = css.split(".language {", 1)[1].split("}", 1)[0]
    select = css.split(".language select {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in header
    for block, name in ((row, ".language"), (select, ".language select")):
        for forbidden in ("max-inline-size", "overflow: hidden", "text-overflow"):
            assert forbidden not in block, f"{name}: {forbidden}"


def test_the_boot_decides_the_language_before_the_first_paint() -> None:
    """No English flash: the locale is chosen and loaded before any text reaches the screen, and
    the override is read after the options arrive, since it lives under the app's own slug."""
    body = app_source().split("async function boot() {", 1)[1].split("\nboot();", 1)[0]
    assert body.count("openingLocale()") == 1
    assert body.index("await requestJson(ENDPOINTS.options);") < body.index("openingLocale()")
    assert body.index("openingLocale()") < body.rindex("applyText(document);")
    # The failure path still needs a locale before it can say anything, and it says it in English,
    # since the only thing that answers in that path is the code of the failed request itself.
    assert body.index("await tryLocale(DEFAULT_LOCALE);") < body.index("showError(failure);")
