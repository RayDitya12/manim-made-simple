"""The interface, served by the backend (ROADMAP task 3.1, SPEC.md section 4 stage 1).

SPEC.md section 4 develops the product in stages and stage one is a local web app: FastAPI
plus plain HTML/CSS/JS, started from a terminal and opened in a browser. This module is what
turns ``python -m backend.app.main`` into "open http://127.0.0.1:8000 and use the tool",
before the Electron shell arrives in ROADMAP 6.1.

The request path is never turned into a filesystem path. :data:`RESOURCES` is a constant
table of the exact URLs the interface lives at, and each entry names the file to send as a
literal written in this file. A URL that is not one of those seven strings never reaches a
lookup, so there is no joined path to traverse, no encoded separator to decode, and no
symlink to follow. The same table also means the routes and the files cannot drift: the
loop in :func:`install` registers exactly the paths the table advertises.

Two headers come along with every interface response. ``Content-Security-Policy`` is the
same-origin, script-from-this-app-only policy AGENTS.md section 4 asks the Electron window to
carry, set here so the browser workflow runs under the policy the packaged app will use --
if the UI ever grows an inline handler, it breaks in the terminal stage and not in Phase 6.
``X-Content-Type-Options: nosniff`` stops a browser from re-reading a stylesheet as a script.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response

#: Where the static interface lives in the repository. The UI is not a package: it is plain
#: files, so Python never imports it and this path is the only link between the two halves.
UI_DIR = Path(__file__).resolve().parents[2] / "ui"

#: The media type of each kind of file the UI is made of. ES modules go out as
#: ``text/javascript`` or the browser refuses to ``import`` them.
MEDIA_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

#: Every URL the interface answers, mapped to ``(file inside ui/, media type)``.
#: A path is matched exactly, including its leading slash, and nothing is derived from it.
RESOURCES: dict[str, tuple[str, str]] = {
    "/": ("index.html", MEDIA_TYPES[".html"]),
    "/index.html": ("index.html", MEDIA_TYPES[".html"]),
    "/styles.css": ("styles.css", MEDIA_TYPES[".css"]),
    "/app.js": ("app.js", MEDIA_TYPES[".js"]),
    "/i18n.js": ("i18n.js", MEDIA_TYPES[".js"]),
    "/locales/en.json": ("locales/en.json", MEDIA_TYPES[".json"]),
    "/locales/id.json": ("locales/id.json", MEDIA_TYPES[".json"]),
}

#: Script, style, media and ``fetch`` may only come from this origin. The UI has no other
#: source: it is a local tool with no network access at run time (SPEC.md section 10), so a
#: policy that allows anything else would only ever allow an injection.
CSP_HEADER = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "media-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "object-src 'none'",
    )
)

#: Development reloads must show an edit, not a cached copy of the file the user just fixed.
CACHE_CONTROL_HEADER = "no-cache"

#: The SPEC.md section 6 error object for an interface file that is not on disk. That is an
#: installation problem rather than anything the request caused, so it is not a 404.
MISSING_FILE_MESSAGE = "the interface file is missing from this installation."

#: A URL that is not in the table. Nothing is read, not even to say the file does not exist.
UNROUTED_MESSAGE = "no interface is served at this address."


def path_for(url_path: str) -> tuple[Path, str] | None:
    """The file and media type one interface URL is allowed to mean.

    Returns ``None`` for a URL that is not in :data:`RESOURCES`. The lookup is the only use
    this function makes of its argument: the returned path comes from the table, never from
    the request.
    """
    entry = RESOURCES.get(url_path)
    if entry is None:
        return None
    relative, media_type = entry
    return UI_DIR / relative, media_type


def _error(status_code: int, code: str, message: str) -> Response:
    """An answer in the SPEC.md section 6 error shape, for the interface layer."""
    return Response(
        status_code=status_code,
        media_type="application/json",
        content=json.dumps(
            {"error": {"code": code, "message": message, "params": {}}},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers=_headers(),
    )


def response_for(url_path: str) -> Response:
    """Serve one interface URL.

    Two answers come from here rather than from a file: a URL outside :data:`RESOURCES`, and
    a URL in the table whose file is not on disk. :func:`install` only registers routes for
    the table, so the first is unreachable through the app and exists to keep
    :func:`response_for` total on its own.
    """
    resolved = path_for(url_path)
    if resolved is None:
        return _error(404, "ui_url_unknown", UNROUTED_MESSAGE)
    file_path, media_type = resolved
    if not file_path.is_file():
        return _error(500, "ui_file_missing", MISSING_FILE_MESSAGE)
    # ``FileResponse`` streams and answers ``Last-Modified``/``ETag``, so a reload of the page
    # only re-pulls the files that actually changed.
    return FileResponse(file_path, media_type=media_type, headers=_headers())


def _headers() -> dict[str, str]:
    return {
        "Content-Security-Policy": CSP_HEADER,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": CACHE_CONTROL_HEADER,
    }


def install(app: FastAPI) -> None:
    """Register one ``GET`` route per interface URL.

    Called after :func:`backend.app.security.install`, so a request with a non-loopback
    ``Host`` is refused before the interface is served (SPEC.md section 10). No catch-all
    route is added: an unknown path stays the framework's 404, which is what keeps
    ``/health`` and ``/api/nope`` as "no such endpoint" answers rather than the homepage.
    """

    def make_handler(url_path: str) -> Any:
        def handler() -> Response:
            return response_for(url_path)

        return handler

    for url_path in RESOURCES:
        app.add_api_route(
            url_path,
            make_handler(url_path),
            methods=["GET"],
            include_in_schema=False,
        )


__all__ = [
    "CACHE_CONTROL_HEADER",
    "CSP_HEADER",
    "MEDIA_TYPES",
    "MISSING_FILE_MESSAGE",
    "RESOURCES",
    "UI_DIR",
    "UNROUTED_MESSAGE",
    "install",
    "path_for",
    "response_for",
]
