"""Environment self-check for Manim Made Simple (ROADMAP task 1.1).

Verifies the interpreter, Manim Community Edition, and FFmpeg are usable,
printing each version. Exits non-zero with a clear message when something is
missing. Run it with the same Python that the backend will use, e.g. from an
activated virtualenv:

    python scripts/check_env.py

Standard library only; no new dependencies.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

MIN_PYTHON = (3, 11)
MANIM_IMPORT_NAME = "manim"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single environment check."""

    name: str
    ok: bool
    detail: str
    hint: str = ""


def check_python(version_info: tuple[int, ...] | None = None) -> CheckResult:
    """Confirm the running interpreter is at least ``MIN_PYTHON``."""

    info = version_info if version_info is not None else sys.version_info
    version_text = ".".join(str(part) for part in info[:3])
    if tuple(info[:2]) < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        return CheckResult(
            "Python",
            ok=False,
            detail=f"{version_text} is older than {required}",
            hint=f"Install Python {required} or newer and run this script with it.",
        )
    return CheckResult("Python", ok=True, detail=version_text)


def check_manim() -> CheckResult:
    """Confirm Manim Community Edition is importable and report its version."""

    try:
        import manim
    except ImportError:
        return CheckResult(
            "Manim CE",
            ok=False,
            detail="the 'manim' package is not importable",
            hint="Install it with 'python -m pip install -r requirements.txt'.",
        )
    version = getattr(manim, "__version__", "unknown")
    return CheckResult("Manim CE", ok=True, detail=str(version))


def _parse_ffmpeg_version(output: str) -> str:
    """Pull the version token out of the ``ffmpeg -version`` header line.

    The first line looks like ``ffmpeg version 6.1.1 Copyright (c) ...``.
    """

    first_line = output.splitlines()[0] if output.splitlines() else ""
    parts = first_line.split()
    # Find the token that follows the word "version".
    for index, token in enumerate(parts):
        if token == "version" and index + 1 < len(parts):
            return parts[index + 1]
    return "unknown"


def check_ffmpeg(runner=subprocess.run) -> CheckResult:
    """Confirm an FFmpeg binary is on PATH and report its version."""

    executable = shutil.which("ffmpeg")
    if executable is None:
        return CheckResult(
            "FFmpeg",
            ok=False,
            detail="'ffmpeg' was not found on PATH",
            hint="Install FFmpeg and make sure 'ffmpeg' is on PATH (packaged "
            "builds bundle their own; see ROADMAP task 6.3).",
        )
    try:
        completed = runner(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult(
            "FFmpeg",
            ok=False,
            detail=f"found '{executable}' but running it failed: {exc}",
            hint="Reinstall FFmpeg or fix the 'ffmpeg' entry on PATH.",
        )
    if completed.returncode != 0:
        return CheckResult(
            "FFmpeg",
            ok=False,
            detail=f"'{executable}' exited with code {completed.returncode}",
            hint="Reinstall FFmpeg or fix the 'ffmpeg' entry on PATH.",
        )
    version = _parse_ffmpeg_version(completed.stdout)
    return CheckResult("FFmpeg", ok=True, detail=f"{version} ({executable})")


def run_checks() -> list[CheckResult]:
    """Run every check and return their results in order."""

    return [check_python(), check_manim(), check_ffmpeg()]


def format_line(result: CheckResult) -> str:
    status = "ok" if result.ok else "MISSING"
    line = f"[{status:^7}] {result.name:<9} {result.detail}"
    if not result.ok and result.hint:
        line += f"\n            -> {result.hint}"
    return line


def main() -> int:
    """Print all check results and return a process exit code."""

    print("Manim Made Simple - environment check")
    print("")
    results = run_checks()
    for result in results:
        print(format_line(result))

    missing = [result for result in results if not result.ok]
    print("")
    if missing:
        print(f"Result: {len(missing)} of {len(results)} checks FAILED.")
        return 1
    print(f"Result: all {len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
