"""Render cache: key, file name, lookup, and expiry (ROADMAP task 2.2).

SPEC.md section 6 fixes the rule: the cache key is the SHA-256 of the canonical JSON
of the *validated* parameters, the app version, the Manim version, and the template
version, and a rendered file is named ``<sha256>-<quality>.<ext>``. Two consequences
shape this module:

*   The key is taken from :class:`~backend.app.models.RenderParams`, not from the raw
    request. Filling in the defaults means ``{"text": "Hi"}`` and
    ``{"text": "Hi", "font_size": 96}`` are the same render and share one file, while a
    field that actually changes the video always changes the key.
*   The versions belong in the key because they change the pixels the parameters
    describe: a new Manim release, a new template, or a new app build must all miss the
    old cache rather than serve it.

Files are keyed by content, so a key is a name, not a secret: nothing user-typed ever
appears in a path. The name is rebuilt from a strict pattern before it touches the
filesystem, and the resolved path is checked to stay inside the output directory.

This module does no rendering and reads no request. Deciding *when* to serve a cached
file is the job queue's (ROADMAP 2.3); keeping a hostile file name out of the directory
is the video endpoint's (ROADMAP 2.4).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from backend.app import app_meta
from backend.app.models import RenderParams, parse_params
from backend.app.render import OUTPUT_EXTENSION, QUALITY_PRESETS, TEMPLATE_VERSION
from scripts import check_env

logger = logging.getLogger("mms.cache")

#: How long a cached video survives before startup cleanup removes it (SPEC.md section 6).
MAX_AGE_SECONDS = 7 * 24 * 60 * 60

KEY_HEX_LENGTH = 64
KEY_PATTERN = re.compile(rf"^[0-9a-f]{{{KEY_HEX_LENGTH}}}$")
QUALITY_IDS = tuple(QUALITY_PRESETS)

#: One cached video file name, used to recognise cache files without touching anything
#: else that happens to live in the output directory (logs, downloaded updates, ...).
#: The two groups are the key and the quality, so the video endpoint (ROADMAP 2.4) can read
#: a name back into the arguments :func:`video_path` wants instead of parsing it by hand.
VIDEO_NAME_PATTERN = re.compile(
    rf"^([0-9a-f]{{{KEY_HEX_LENGTH}}})-({'|'.join(re.escape(q) for q in QUALITY_IDS)})"
    rf"\.{re.escape(OUTPUT_EXTENSION)}$"
)


class CacheError(ValueError):
    """A cache operation was asked for something outside its rules.

    Carries a stable SPEC.md section 6 error code, an English fallback message, and the
    numbers a translated sentence needs. No user input is put in a message.
    """

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.params: dict[str, Any] = dict(params or {})


def installed_manim_version() -> str:
    """The Manim CE version whose rendering the key describes.

    A missing Manim is a hard error here rather than a silent placeholder: a key built
    from an unknown version would be served files it cannot prove it produced.
    """
    version = check_env.check_manim().version
    if not version:
        raise CacheError("manim_missing", "the installed Manim version could not be determined.")
    return version


def canonical_json(document: Mapping[str, Any]) -> str:
    """Serialize with sorted keys and no incidental whitespace, so equal data is equal text."""
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def cache_key(
    params: RenderParams | Mapping[str, Any],
    *,
    app_version: str | None = None,
    manim_version: str | None = None,
    template_version: str = TEMPLATE_VERSION,
) -> str:
    """Return the 64-character lowercase hex key for one render.

    ``params`` may be a raw request or an already validated
    :class:`~backend.app.models.RenderParams`; both are normalized through the model, so
    equivalent parameters always produce one key. The three versions default to what this
    machine and this build actually have, and can be passed explicitly by a test or by a
    caller that already resolved them. Raises
    :class:`~backend.app.models.RenderParamsError` when the parameters are outside
    SPEC.md section 5 and :class:`CacheError` when the Manim version is unknowable.
    """
    validated = params if isinstance(params, RenderParams) else parse_params(dict(params))
    document = {
        "params": validated.model_dump(),
        "app_version": app_version if app_version is not None else app_meta.app_version(),
        "manim_version": manim_version if manim_version is not None else installed_manim_version(),
        "template_version": template_version,
    }
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def video_stem(key: str, quality: str) -> str:
    """``<sha256>-<quality>``, the name of a cached video without its extension.

    :func:`backend.app.render.render_video` takes a stem rather than a file name, so the
    job queue uses this to ask Manim for exactly the file the key describes.
    """
    return f"{key}-{quality}"


def video_file_name(key: str, quality: str) -> str:
    """Return ``<sha256>-<quality>.<ext>``, the only name a cached video may have."""
    if not KEY_PATTERN.fullmatch(key):
        raise CacheError(
            "cache_key_invalid",
            f"a cache key must be {KEY_HEX_LENGTH} hexadecimal characters.",
            {"length": KEY_HEX_LENGTH},
        )
    if quality not in QUALITY_PRESETS:
        raise CacheError(
            "quality_invalid",
            f"quality must be one of {', '.join(QUALITY_IDS)}.",
            {"field": "quality", "allowed": list(QUALITY_IDS)},
        )
    return f"{video_stem(key, quality)}.{OUTPUT_EXTENSION}"


def video_path(output_dir: Path, key: str, quality: str) -> Path:
    """Return where the cached video for ``key`` lives, guaranteed inside ``output_dir``."""
    name = video_file_name(key, quality)
    directory = Path(output_dir)
    candidate = (directory / name).resolve()
    if not candidate.is_relative_to(directory.resolve()):
        # Unreachable with the pattern above, kept as the seam the 2.4 endpoint relies on.
        raise CacheError("cache_path_outside_output_dir", "the video path escapes the cache.")
    return candidate


def is_cached(path: Path) -> bool:
    """Is ``path`` a usable cached video?

    An empty file does not count: a render killed part-way can leave one behind, and
    serving it would show the user a broken video instead of a fresh render.
    """
    return path.is_file() and path.stat().st_size > 0


def find_cached(
    output_dir: Path,
    params: RenderParams | Mapping[str, Any],
    quality: str | None = None,
    **key_kwargs: Any,
) -> Path | None:
    """Return the cached video for these parameters, or ``None`` when it must be rendered.

    ``quality`` defaults to the quality inside ``params``, the normal case. It is passed
    only when a caller renders one parameter set at another quality, and a mismatch is
    refused instead of guessed: ``quality`` is part of the key, so pairing one quality's
    key with another quality's file name would create a path that cannot be reproduced.
    """
    validated = params if isinstance(params, RenderParams) else parse_params(dict(params))
    if quality is None:
        quality = validated.quality
    elif quality != validated.quality:
        raise CacheError(
            "quality_mismatch",
            "the requested quality differs from the quality in the parameters.",
            {"requested": quality, "params": validated.quality},
        )
    path = video_path(output_dir, cache_key(validated, **key_kwargs), quality)
    return path if is_cached(path) else None


def cleanup_stale_videos(
    output_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = MAX_AGE_SECONDS,
) -> list[Path]:
    """Delete cache files older than ``max_age_seconds``; return what was removed.

    Runs on startup (SPEC.md section 6). Only names matching :data:`VIDEO_NAME_PATTERN`
    are considered, so render logs and anything else in the directory are left alone. A
    file that cannot be removed is logged and skipped: a stale cache entry is not a
    reason to refuse to start.
    """
    directory = Path(output_dir)
    if not directory.is_dir():
        return []
    reference = time.time() if now is None else now
    removed: list[Path] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not VIDEO_NAME_PATTERN.fullmatch(path.name):
            continue
        try:
            age = reference - path.stat().st_mtime
        except OSError as exc:
            logger.warning("could not stat %s: %s", path.name, exc)
            continue
        if age <= max_age_seconds:
            continue
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("could not remove the stale cache file %s: %s", path.name, exc)
            continue
        removed.append(path)
    if removed:
        logger.info("removed %d stale cache file(s) from %s", len(removed), directory)
    return removed


__all__ = [
    "KEY_HEX_LENGTH",
    "KEY_PATTERN",
    "MAX_AGE_SECONDS",
    "QUALITY_IDS",
    "VIDEO_NAME_PATTERN",
    "CacheError",
    "cache_key",
    "canonical_json",
    "cleanup_stale_videos",
    "find_cached",
    "installed_manim_version",
    "is_cached",
    "video_file_name",
    "video_path",
    "video_stem",
]
