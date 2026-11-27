"""Host and origin hardening (ROADMAP task 2.5, SPEC.md section 10).

SPEC.md section 10 states three rules for a server that only ever talks to its own UI:
bind ``127.0.0.1``, send CORS headers only to the localhost UI origin, and reject an
unexpected ``Host`` header. The first is a uvicorn argument (:data:`LOOPBACK_HOST`, used by
:func:`backend.app.main.main` and by the Electron launcher in ROADMAP 6.1). The other two
live here.

Why the ``Host`` check is not paranoia on a loopback-only server: a page on the internet can
make a request to ``http://127.0.0.1:port`` (the visitor's own machine resolves to itself),
and DNS rebinding lets an attacker's hostname resolve to ``127.0.0.1`` outright. The header is
what separates "this came from the UI" from "a webpage the user visited asked for this", so
anything that is not loopback is refused before a route runs.

Two deliberate choices:

*   The guard is a **pure ASGI** middleware rather than ``BaseHTTPMiddleware``. The video
    endpoint streams a file with ``FileResponse``, and the base class shuttles chunks between
    threads, which is the last thing a download wants.
*   The failure answer is the SPEC.md section 6 error object, not the framework's plain-text
    400, so a rejection has a stable code like every other API error.

Cross-origin handling is Starlette's own ``CORSMiddleware`` limited to one regex: a
hand-rolled CORS policy is a well-known way to get it subtly wrong.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

#: The only address the backend binds (SPEC.md section 10). Not configurable: a backend
#: reachable from the LAN is a different product with different requirements.
LOOPBACK_HOST = "127.0.0.1"

#: Host names a browser may legitimately put in a ``Host`` header for a loopback server.
#: ``localhost`` is here because the terminal workflow is usually opened that way.
ALLOWED_HOST_NAMES: frozenset[str] = frozenset({LOOPBACK_HOST, "localhost", "::1"})

#: Origins CORS may name. Any port, because Electron takes a free one at run time
#: (ROADMAP 6.1) and the dev server's port is whichever one is unoccupied. No ``https``: this
#: server has no certificate, so an https origin would be a lie.
UI_ORIGIN_REGEX = r"http://(127\.0\.0\.1|localhost)(:\d{1,5})?"

#: The API answers these; a preflight for anything else is refused by CORS middleware.
ALLOWED_METHODS: tuple[str, ...] = ("GET", "POST")

#: Request headers the UI sends: JSON bodies, plus the range header a player uses.
ALLOWED_HEADERS: tuple[str, ...] = ("content-type", "range")

#: The message a rejected ``Host`` gets. No part of the request is echoed into it.
HOST_REJECTED = "this endpoint only answers its own interface."


def host_parts(value: str) -> tuple[str, str]:
    """Split a ``Host`` header into ``(name, port)``, lower-casing the name.

    An IPv6 literal keeps its brackets out of the name: ``[::1]:8000`` is ``("::1", "8000")``.
    A name with no port yields the raw text as the name and an empty port.
    """
    text = value.strip()
    if text.startswith("["):
        name, closing, rest = text.partition("]")
        if not closing or (rest and not rest.startswith(":")):
            # An unterminated IPv6 literal, or digits glued to the bracket, is a malformed
            # header rather than a host to guess at.
            return "", ""
        port = rest[1:] if rest.startswith(":") else rest
        return name[1:].lower(), port
    name, separator, port = text.rpartition(":")
    if not separator:
        return text.lower(), ""
    return name.lower(), port


def is_allowed_host(value: str | None) -> bool:
    """Is this ``Host`` header value the loopback server answering its own UI?"""
    if not value:
        return False
    name, port = host_parts(value)
    if name not in ALLOWED_HOST_NAMES:
        return False
    return not port or port.isdigit()


class HostGuard:
    """Pure ASGI middleware that answers a non-loopback ``Host`` with 400.

    The route never runs for a refused request, so a rogue page cannot queue a render, read a
    video, or learn anything from a status code.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._host_ok(scope):
            await self.app(scope, receive, send)
            return
        body = json.dumps(
            {"error": {"code": "host_not_allowed", "message": HOST_REJECTED, "params": {}}},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    def _host_ok(scope: Scope) -> bool:
        """Exactly one ``Host`` header, and it names loopback.

        More than one is an ambiguous request, not a second chance to match.
        """
        headers = [value for name, value in scope.get("headers", []) if name == b"host"]
        if len(headers) != 1:
            return False
        return is_allowed_host(headers[0].decode("latin-1"))


def install(app: Any) -> None:
    """Put the origin policy and the host guard on the app, guard last.

    Starlette treats the middleware added most recently as the outermost layer, so the
    ``Host`` check wins: a rebinding attempt is refused without ever being told which origins
    this server considers same-site.
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=UI_ORIGIN_REGEX,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        allow_credentials=False,
    )
    app.add_middleware(HostGuard)


__all__ = [
    "ALLOWED_HEADERS",
    "ALLOWED_HOST_NAMES",
    "ALLOWED_METHODS",
    "HOST_REJECTED",
    "HostGuard",
    "LOOPBACK_HOST",
    "UI_ORIGIN_REGEX",
    "host_parts",
    "install",
    "is_allowed_host",
]
