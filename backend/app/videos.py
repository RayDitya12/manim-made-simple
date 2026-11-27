"""The video endpoint: name in, bytes out (ROADMAP task 2.4, SPEC.md sections 6 and 10).

SPEC.md section 6 defines ``GET /api/videos/{name}`` as the way the UI gets a rendered
file, with ``?download=1`` asking for it as an attachment. Because the backend binds to
``127.0.0.1`` and the URL arrives from a browser, the name is untrusted input that is
about to be turned into a filesystem path, which makes this the one endpoint where a
mistake reads a file the app never rendered. The defense is that the name is never used
as a path:

1. :func:`split_name` matches the whole name against :data:`backend.app.cache.VIDEO_NAME_PATTERN`,
   i.e. 64 lowercase hex characters, a known quality id, and the one output extension. A
   dot, a slash, a backslash, an encoded slash, a second extension, or a stray character
   cannot survive that match.
2. The two pieces are handed back to :func:`backend.app.cache.video_path`, which rebuilds
   the name from them and resolves the result inside the output directory (cache.py). The
   path the response uses is therefore derived from a value this module reconstructed, not
   from the request.
3. :func:`backend.app.cache.is_cached` then requires the file to exist and be non-empty, so
   a render killed part-way is reported missing instead of streamed as a broken video.

Anything that does not survive those steps is answered with a stable SPEC.md section 6
error code, and the name is never echoed back into a message.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse

from backend.app import cache

#: The media type of a rendered video. ``mp4`` is what
#: :data:`backend.app.render.OUTPUT_EXTENSION` names the file with, and a test asserts the
#: two still agree, so this cannot quietly drift away from what the renderer writes.
VIDEO_MEDIA_TYPE = "video/mp4"

#: The one query value SPEC.md section 6 defines for a downloadable response.
DOWNLOAD_QUERY_VALUE = "1"

#: The sentence both video routes give for a name the cache could not have produced. No part
#: of the request is interpolated into it, so nothing user-typed comes back out.
NAME_INVALID_MESSAGE = "the video name must be a file name this app rendered."


class VideoError(ValueError):
    """A video request that cannot be served, with a SPEC.md section 6 error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


#: HTTP status per error code, so the endpoint answers an unusable name with 400 and a
#: video that is not on disk with the same 404 any other missing resource gets.
ERROR_STATUS: dict[str, int] = {
    "video_name_invalid": 400,
    "video_not_found": 404,
}


def split_name(name: str) -> tuple[str, str]:
    """Return ``(cache_key, quality)`` for a name this app could have rendered.

    Raises :class:`VideoError` (``video_name_invalid``) otherwise. The input is matched in
    full and discarded afterwards: only the two reconstructed pieces leave this function.
    """
    parts = cache.VIDEO_NAME_PATTERN.fullmatch(name)
    if parts is None:
        raise VideoError("video_name_invalid", NAME_INVALID_MESSAGE)
    return parts.group(1), parts.group(2)


def resolve(output_dir: Path, name: str) -> Path:
    """The path inside ``output_dir`` that ``name`` is allowed to mean.

    Reconstruction goes through :func:`backend.app.cache.video_path`, so a name that is not
    a cache file name is refused there too, and the returned path is checked to stay inside
    the output directory before the caller may stat it.
    """
    key, quality = split_name(name)
    return cache.video_path(Path(output_dir), key, quality)


def serve(output_dir: Path, name: str, *, download: bool = False) -> FileResponse:
    """Build the response for one video name, or raise :class:`VideoError`.

    ``download`` selects ``Content-Disposition: attachment``; the inline form is what the
    player uses. Starlette's ``FileResponse`` streams in chunks, answers ``HEAD`` without a
    body, and serves ``Range`` requests, which is what lets the UI seek (SPEC.md section 8).
    """
    path = resolve(output_dir, name)
    if not cache.is_cached(path):
        raise VideoError("video_not_found", "no rendered video with this name is in the cache.")
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path,
        media_type=VIDEO_MEDIA_TYPE,
        filename=path.name,
        content_disposition_type=disposition,
    )


__all__ = [
    "DOWNLOAD_QUERY_VALUE",
    "ERROR_STATUS",
    "NAME_INVALID_MESSAGE",
    "VIDEO_MEDIA_TYPE",
    "VideoError",
    "resolve",
    "serve",
    "split_name",
]
