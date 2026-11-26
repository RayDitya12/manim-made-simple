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


def test_ffmpeg_not_on_path(check_env, monkeypatch) -> None:
    monkeypatch.setattr(check_env.shutil, "which", lambda _name: None)
    result = check_env.check_ffmpeg()
    assert not result.ok
    assert "not found" in result.detail


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
