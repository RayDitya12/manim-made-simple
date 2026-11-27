"""Tests for backend/app/data_dir.py (ROADMAP task 2.3, SPEC.md section 6).

The rule the spec gives is one sentence -- the output directory is ``MTS_DATA_DIR``, which
Electron sets to its ``userData`` -- and everything else here is what that sentence has to
mean on each of the three OSes this app ships on. The OS conventions are checked by passing
the OS name in, because a developer only has one of them available locally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app import app_meta, data_dir


def test_mts_data_dir_env_var_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, str(tmp_path / "electron-user-data"))
    assert data_dir.video_dir() == (tmp_path / "electron-user-data" / "videos").resolve()


def test_a_blank_env_var_is_not_a_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, "   ")
    assert data_dir.data_dir().name == app_meta.app_slug()


def test_a_relative_env_var_becomes_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, "scratch-cache")
    assert data_dir.data_dir().is_absolute()
    assert data_dir.data_dir().name == "scratch-cache"


def test_the_fallback_follows_each_os_convention(tmp_path: Path) -> None:
    """Windows, macOS and Linux each have a documented place for per-user data."""
    slug = app_meta.app_slug()
    env = {
        "LOCALAPPDATA": str(tmp_path / "AppData"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
    }
    windows = data_dir._default_data_dir(env, os_name="nt", sys_platform="win32")
    macos = data_dir._default_data_dir(env, os_name="posix", sys_platform="darwin")
    linux = data_dir._default_data_dir(env, os_name="posix", sys_platform="linux")
    assert windows == tmp_path / "AppData" / slug
    assert macos == tmp_path / "home" / "Library" / "Application Support" / slug
    assert linux == tmp_path / "xdg" / slug


def test_the_fallback_survives_a_missing_env_var(tmp_path: Path) -> None:
    """Only HOME is set here: each OS still has a documented home-relative layout."""
    slug = app_meta.app_slug()
    env = {"HOME": str(tmp_path / "home")}
    assert data_dir._default_data_dir(env, os_name="nt", sys_platform="win32") == (
        tmp_path / "home" / "AppData" / "Local" / slug
    )
    assert data_dir._default_data_dir(env, os_name="posix", sys_platform="linux") == (
        tmp_path / "home" / ".local" / "share" / slug
    )


def test_the_video_dir_is_a_folder_inside_the_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(data_dir.DATA_DIR_ENV_VAR, raising=False)
    video = data_dir.video_dir()
    assert video.is_absolute()
    assert video.parent == data_dir.data_dir()
    assert video.name == data_dir.VIDEO_DIR_NAME


def test_nothing_is_created_until_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "not-yet"
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, str(target))
    assert data_dir.video_dir() == (target / "videos").resolve()
    assert not target.exists()


def test_ensure_video_dir_creates_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, str(tmp_path / "deep" / "nest"))
    created = data_dir.ensure_video_dir()
    assert created == (tmp_path / "deep" / "nest" / "videos").resolve()
    assert created.is_dir()
    assert data_dir.ensure_video_dir() == created


def test_describe_reports_where_the_cache_is_and_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(data_dir.DATA_DIR_ENV_VAR, str(tmp_path))
    report = data_dir.describe()
    assert report["source"] == "env"
    assert report["video_dir"] == str((tmp_path / "videos").resolve())
    monkeypatch.delenv(data_dir.DATA_DIR_ENV_VAR)
    assert data_dir.describe()["source"] == "default"
