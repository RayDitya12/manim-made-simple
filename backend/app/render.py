"""Render function: parameters in, MP4 out (ROADMAP task 1.4, SPEC.md section 7).

The pipeline is fixed by the spec and has exactly three steps:

1. validate a raw parameter document with :func:`backend.app.models.parse_params`;
2. write it as JSON and hand the template its path through ``MTS_PARAMS``, so no
   value is ever interpolated into a command string or into source code;
3. run Manim as a child process from an argument list, with the quality preset's
   timeout, and move the finished MP4 into the caller's output directory.

``params`` is a plain mapping, not a model: callers validate at their own edge and
pass ``RenderParams.model_dump()``. The template re-validates the same document a
second time inside its own process.

Only ``sys.executable`` and the paths built here reach the command line; the user's
text, colors, and font id never do. Rendering happens in a throwaway working
directory so Manim's partial movie files and logs stay out of the output directory,
which is reserved for the videos the API serves (SPEC.md section 6).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.models import RenderParamsError, parse_params

logger = logging.getLogger("mms.render")

PARAMS_ENV_VAR = "MTS_PARAMS"
SCENE_PATH = Path(__file__).resolve().parents[1] / "scenes" / "text_scene.py"
SCENE_CLASS = "MmsTextScene"
OUTPUT_EXTENSION = "mp4"

#: The executable Manim is launched from. ROADMAP 6.4 points this at the bundled
#: Python runtime; during development it is the interpreter running the backend.
MANIM_COMMAND: tuple[str, ...] = (sys.executable, "-m", "manim")

FILE_STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
LOG_TAIL_CHARS = 20_000
WAIT_AFTER_KILL_SECONDS = 10.0


@dataclass(frozen=True)
class QualityPreset:
    """One SPEC.md section 5 quality value, with its render timeout."""

    width: int
    height: int
    fps: int
    timeout_seconds: float


QUALITY_PRESETS: dict[str, QualityPreset] = {
    "preview": QualityPreset(width=854, height=480, fps=15, timeout_seconds=60.0),
    "hd": QualityPreset(width=1920, height=1080, fps=60, timeout_seconds=120.0),
}


class RenderError(RuntimeError):
    """Rendering produced no video. Carries a stable SPEC.md section 6 error code."""

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.params: dict[str, Any] = dict(params or {})


def render_video(params: Mapping[str, Any], output_dir: Path, file_stem: str) -> Path:
    """Validate ``params``, render them with Manim, and return the MP4 path.

    The finished clip is written to ``output_dir / f"{file_stem}.mp4"`` and returned.
    Raises :class:`RenderParamsError` for input outside SPEC.md section 5 and
    :class:`RenderError` when the render itself does not produce a file.
    """
    validated = parse_params(dict(params))
    preset = QUALITY_PRESETS[validated.quality]
    stem = _validated_stem(file_stem)
    destination_dir = Path(output_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{stem}.{OUTPUT_EXTENSION}"

    work_dir = Path(tempfile.mkdtemp(prefix="mms-render-"))
    try:
        params_path = work_dir / "params.json"
        params_path.write_text(
            json.dumps(validated.model_dump(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        media_dir = work_dir / "media"
        command = build_manim_command(preset, media_dir, f"{stem}.{OUTPUT_EXTENSION}")
        outcome = _run_manim(command, params_path, work_dir, preset.timeout_seconds)
        if outcome.timed_out:
            raise _failure(
                destination_dir,
                stem,
                outcome.output,
                "render_timeout",
                f"the render took longer than {preset.timeout_seconds:g} seconds and was stopped.",
                {"limit": preset.timeout_seconds},
            )
        if outcome.returncode != 0:
            raise _failure(
                destination_dir,
                stem,
                outcome.output,
                "render_failed",
                "the video could not be rendered.",
            )
        produced = _find_output(media_dir, f"{stem}.{OUTPUT_EXTENSION}")
        if produced is None:
            raise _failure(
                destination_dir,
                stem,
                outcome.output,
                "render_output_missing",
                "the video file could not be created.",
            )
        shutil.move(str(produced), str(destination))
        logger.info("rendered %s (%s quality)", destination, validated.quality)
        return destination
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _failure(
    output_dir: Path,
    stem: str,
    output: str,
    code: str,
    message: str,
    params: dict[str, Any] | None = None,
) -> RenderError:
    """Turn a bad Manim run into a user-safe error, keeping the detail in a log."""
    log_path = _write_failure_log(output_dir, stem, output)
    logger.error("%s: manim output kept in %s", code, log_path)
    return RenderError(code, message, params)


def build_manim_command(
    preset: QualityPreset,
    media_dir: Path,
    file_name: str,
    scene_path: Path = SCENE_PATH,
) -> list[str]:
    """Return the Manim argument list. Never a shell string (AGENTS.md section 5)."""
    return [
        *MANIM_COMMAND,
        "render",
        "--verbosity",
        "WARNING",
        "--disable_caching",
        "--media_dir",
        str(media_dir),
        "--format",
        OUTPUT_EXTENSION,
        "-r",
        f"{preset.width},{preset.height}",
        "--fps",
        str(preset.fps),
        "-o",
        file_name,
        str(scene_path),
        SCENE_CLASS,
    ]


def _validated_stem(file_stem: str) -> str:
    """Reject anything that could escape the output directory when joined onto it."""
    if not FILE_STEM_PATTERN.fullmatch(file_stem):
        raise RenderError(
            "file_name_invalid",
            "the output name may only contain letters, digits, dot, dash and underscore.",
        )
    return file_stem


@dataclass(frozen=True)
class _RunOutcome:
    """What one Manim child process did: exit code, output, and whether it was killed."""

    returncode: int
    output: str
    timed_out: bool = False


def _run_manim(
    command: list[str],
    params_path: Path,
    work_dir: Path,
    timeout_seconds: float,
) -> _RunOutcome:
    """Run the Manim child process and report its exit code and combined output."""
    popen_kwargs: dict[str, Any] = {
        "cwd": str(work_dir),
        "env": {**os.environ, PARAMS_ENV_VAR: str(params_path)},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        # SPEC.md section 7: no console window flashes behind the app on Windows.
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        # Lets a timeout kill the whole process group, not just the leader.
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **popen_kwargs)
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
        return _RunOutcome(process.returncode, output or "")
    except subprocess.TimeoutExpired:
        logger.warning("render timed out after %s s", timeout_seconds)
        return _RunOutcome(-1, _terminate(process), timed_out=True)


def _terminate(process: subprocess.Popen[str]) -> str:
    """Stop a timed-out render and return whatever it managed to print.

    Manim CE 0.22.1 encodes through PyAV inside its own process, so on Windows
    killing the child is enough. On POSIX the process group is killed too, in
    case a renderer step spawns helpers.
    """
    with contextlib.suppress(OSError):
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        # Drains and closes the pipes, so the killed child is reaped.
        output, _ = process.communicate(timeout=WAIT_AFTER_KILL_SECONDS)
        return output or ""
    return ""


def _find_output(media_dir: Path, file_name: str) -> Path | None:
    """Return the finished video inside Manim's media tree, ignoring partial clips.

    Only the path below ``media_dir`` is inspected, so a parent directory that
    happens to contain "partial" in its name cannot hide every result.
    """
    for path in sorted(media_dir.rglob(file_name)):
        relative_parts = path.relative_to(media_dir).parts
        if any("partial" in part for part in relative_parts):
            continue
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def _write_failure_log(output_dir: Path, stem: str, output: str) -> Path | None:
    """Keep Manim's own report on disk; only the error code goes to the user."""
    log_dir = output_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{stem}.log"
        log_path.write_text(output[-LOG_TAIL_CHARS:], encoding="utf-8")
    except OSError as exc:
        logger.warning("could not write the render log: %s", exc)
        return None
    return log_path


__all__ = [
    "MANIM_COMMAND",
    "QUALITY_PRESETS",
    "RenderError",
    "RenderParamsError",
    "SCENE_CLASS",
    "SCENE_PATH",
    "QualityPreset",
    "build_manim_command",
    "render_video",
]
