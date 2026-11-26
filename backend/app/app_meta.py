"""Application identity, read from ``app_meta.json`` at the repository root.

SPEC.md section 3 keeps the product name in exactly one file. ``MMS`` is a
developer shorthand only, so it is never stored here and never reaches a
user-facing string; the value of ``name`` is what an About screen or a page title
shows.

The version lives in the same file so the cache key promised by SPEC.md section
6 can include it without a second place to update. If the file is missing or
malformed the backend still starts: a wrong version only costs a cache miss, and
failing at import time would turn a cosmetic problem into a crash loop.
"""

from __future__ import annotations

import json
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger("mms.app_meta")

META_FILE_NAME = "app_meta.json"
FALLBACK_NAME = "Manim Made Simple"
FALLBACK_SLUG = "manim-made-simple"
FALLBACK_VERSION = "0.0.0"

#: The directory ``app_meta.json`` sits in, i.e. the backend source tree when this
#: is run from an unpacked interpreter.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _candidate_paths() -> tuple[Path, ...]:
    """Where to look for the metadata file, most specific first.

    ``sys.argv[0]`` covers ``python backend/app/main.py`` and a packaged entry
    script; the repository root covers the module form ``uvicorn backend.app.main``.
    """
    from_dir = Path(sys.argv[0]).resolve().parent if sys.argv and sys.argv[0] else None
    paths = [REPO_ROOT / META_FILE_NAME]
    if from_dir is not None:
        paths.insert(0, from_dir / META_FILE_NAME)
    return tuple(dict.fromkeys(paths))


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    for path in _candidate_paths():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(raw, dict):
            return raw
    logger.warning("%s was not readable; using fallback identity", META_FILE_NAME)
    return {}


def _text_field(key: str, fallback: str) -> str:
    value = _load().get(key)
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def app_name() -> str:
    """The user-facing product name."""
    return _text_field("name", FALLBACK_NAME)


def app_slug() -> str:
    """The repository and package slug."""
    return _text_field("slug", FALLBACK_SLUG)


def app_version() -> str:
    """The version string folded into the SPEC.md section 6 cache key."""
    return _text_field("version", FALLBACK_VERSION)


__all__ = ["META_FILE_NAME", "app_name", "app_slug", "app_version"]
