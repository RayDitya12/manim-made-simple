"""Where the app keeps its files (SPEC.md section 6, ROADMAP task 2.3).

The backend needs one writable directory for the videos it caches and the render logs it
writes. SPEC.md section 6 fixes the rule: that directory comes from ``MTS_DATA_DIR``,
which Electron sets to ``app.getPath('userData')`` (ROADMAP task 6.5). Without the
environment variable -- the development workflow of SPEC.md section 4, stage 1 -- it
falls back to the per-user data directory of the host OS, so nothing is ever written
next to the source tree or the executable.

The path is only joined, never built from user input: the values that end up inside it
are cache keys, which :mod:`backend.app.cache` rebuilds from a strict pattern.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from backend.app import app_meta

#: Environment variable Electron (and a developer shell) uses to point the app at its data.
DATA_DIR_ENV_VAR = "MTS_DATA_DIR"

#: Cached videos and render logs live here, inside the data directory.
VIDEO_DIR_NAME = "videos"


def _home(environ: Mapping[str, str]) -> Path:
    value = environ.get("HOME") or environ.get("USERPROFILE")
    return Path(value) if value else Path.home()


def _default_data_dir(
    environ: Mapping[str, str],
    *,
    os_name: str = os.name,
    sys_platform: str = sys.platform,
) -> Path:
    """The per-user data directory for this OS, without creating anything.

    Each layout is the one the OS documents (``%LOCALAPPDATA%``, macOS
    ``~/Library/Application Support``, XDG ``~/.local/share``), read from the environment
    so a non-standard home directory is respected. The two OS arguments exist because this
    app ships on three of them and a developer only has one: the tests below check all
    three conventions from any machine.
    """
    slug = app_meta.app_slug()
    if os_name == "nt":
        base = environ.get("LOCALAPPDATA")
        root = Path(base) if base else _home(environ) / "AppData" / "Local"
    elif sys_platform == "darwin":
        root = _home(environ) / "Library" / "Application Support"
    else:
        base = environ.get("XDG_DATA_HOME")
        root = Path(base) if base else _home(environ) / ".local" / "share"
    return root / slug


def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Return the absolute data directory for this machine.

    ``MTS_DATA_DIR`` wins when it is set to something non-empty; a relative value is made
    absolute against the current directory rather than rejected, because a developer may
    legitimately point it at a scratch path.
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    override = str(env.get(DATA_DIR_ENV_VAR, "")).strip()
    base = Path(override).expanduser() if override else _default_data_dir(env)
    return base.resolve()


def video_dir(environ: Mapping[str, str] | None = None) -> Path:
    """The directory the render cache lives in."""
    return data_dir(environ) / VIDEO_DIR_NAME


def ensure_video_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Create :func:`video_dir` if needed and return it.

    Raises :class:`OSError` when the directory cannot be created, which the caller turns
    into a user-facing message: a data directory that cannot be written means nothing can
    be rendered.
    """
    directory = video_dir(environ)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def describe(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Where the files go and how that was decided, for the startup log line."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    return {
        "source": "env" if str(env.get(DATA_DIR_ENV_VAR, "")).strip() else "default",
        "data_dir": str(data_dir(env)),
        "video_dir": str(video_dir(env)),
    }


__all__ = [
    "DATA_DIR_ENV_VAR",
    "VIDEO_DIR_NAME",
    "data_dir",
    "describe",
    "ensure_video_dir",
    "video_dir",
]
