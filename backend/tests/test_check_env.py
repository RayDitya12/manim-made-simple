"""Tests for scripts/check_env.py (ROADMAP task 1.1)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import types
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_env.py"


@pytest.fixture(scope="module")
def check_env():
    spec = importlib.util.spec_from_file_location("check_env", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_env"] = module
    spec.loader.exec_module(module)
    return module


def test_python_too_old_is_a_failure(check_env) -> None:
    result = check_env.check_python(version_info=(3, 10, 12))
    assert not result.ok
    assert "older" in result.detail
    assert result.hint


def test_python_recent_is_ok(check_env) -> None:
    assert check_env.check_python(version_info=(3, 11, 0)).ok
    assert check_env.check_python(version_info=(3, 12, 10)).ok


def test_manim_missing_reports_clearly(check_env, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "manim", None)
    result = check_env.check_manim()
    assert not result.ok
    assert "requirements.txt" in result.hint


def test_manim_present_reports_version(check_env, monkeypatch) -> None:
    fake = types.ModuleType("manim")
    fake.__version__ = "0.22.1"
    monkeypatch.setitem(sys.modules, "manim", fake)
    result = check_env.check_manim()
    assert result.ok
    assert result.detail == "0.22.1"


def test_ffmpeg_missing_is_a_warning(check_env, monkeypatch) -> None:
    monkeypatch.setattr(check_env.shutil, "which", lambda _name: None)
    monkeypatch.setattr(check_env, "pyav_ffmpeg_version", lambda: "9.0.2")
    result = check_env.check_ffmpeg()
    assert not result.ok
    assert result.warn_only
    assert "9.0.2" in result.detail
    assert "Phase 4" in result.hint


def test_ffmpeg_missing_without_pyav_still_only_warns(check_env, monkeypatch) -> None:
    monkeypatch.setattr(check_env.shutil, "which", lambda _name: None)
    monkeypatch.setattr(check_env, "pyav_ffmpeg_version", lambda: None)
    result = check_env.check_ffmpeg()
    assert not result.ok
    assert result.warn_only
    assert "PyAV" in result.detail
    assert "requirements" in result.hint


def test_pyav_fallback_finds_the_real_library(check_env) -> None:
    """The fallback must work in the venv Manim is installed into."""

    version = check_env.pyav_ffmpeg_version()
    assert version is not None
    assert version[0].isdigit()


def test_ffmpeg_version_parsed(check_env, monkeypatch) -> None:
    monkeypatch.setattr(check_env.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    def fake_runner(cmd, **_kwargs):
        assert cmd[0] == "/usr/bin/ffmpeg"
        return subprocess.CompletedProcess(
            cmd, 0, stdout="ffmpeg version 6.1.1 Copyright (c) the FFmpeg developers\n", stderr=""
        )

    result = check_env.check_ffmpeg(runner=fake_runner)
    assert result.ok
    assert "6.1.1" in result.detail


def test_ffmpeg_nonzero_exit_is_failure(check_env, monkeypatch) -> None:
    monkeypatch.setattr(check_env.shutil, "which", lambda _name: "ffmpeg")

    def fake_runner(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

    assert not check_env.check_ffmpeg(runner=fake_runner).ok


def test_parse_ffmpeg_version_fallback(check_env) -> None:
    assert check_env._parse_ffmpeg_version("") == "unknown"
    assert check_env._parse_ffmpeg_version("banner with no keyword here") == "unknown"


def test_main_exit_code_reflects_failures(check_env, monkeypatch, capsys) -> None:
    ok = check_env.CheckResult("Python", True, "3.12.10")
    bad = check_env.CheckResult("FFmpeg", False, "not found", hint="install it")
    monkeypatch.setattr(check_env, "run_checks", lambda: [ok, bad])
    assert check_env.main() == 1
    printed = capsys.readouterr().out
    assert "MISSING" in printed
    assert "install it" in printed

    monkeypatch.setattr(check_env, "run_checks", lambda: [ok])
    assert check_env.main() == 0
    assert "all 1 checks passed" in capsys.readouterr().out


def test_warning_does_not_fail_the_run(check_env, monkeypatch, capsys) -> None:
    ok = check_env.CheckResult("Python", True, "3.12.10")
    warn = check_env.CheckResult(
        "FFmpeg", False, "no 'ffmpeg' on PATH", hint="install it later", warn_only=True
    )
    monkeypatch.setattr(check_env, "run_checks", lambda: [ok, warn])
    assert check_env.main() == 0
    printed = capsys.readouterr().out
    assert "WARN" in printed
    assert "1 warning" in printed


def test_format_line_labels_each_severity(check_env) -> None:
    assert check_env.format_line(check_env.CheckResult("Manim CE", False, "gone")).startswith(
        "[MISSING]"
    )
    assert check_env.format_line(
        check_env.CheckResult("FFmpeg", False, "no CLI", warn_only=True)
    ).startswith("[ WARN  ]")
    assert check_env.format_line(check_env.CheckResult("Python", True, "3.12.10")).startswith(
        "[  ok   ]"
    )
