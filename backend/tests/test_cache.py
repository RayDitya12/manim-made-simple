"""Tests for the render cache (ROADMAP task 2.2).

The two claims SPEC.md section 6 makes about the key are the two this file exists to
prove: equal parameters produce one key, and any field that changes the video changes it.
The rest covers the file name, the path guard, and the startup cleanup.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app import app_meta, cache
from backend.app.models import RenderParams, RenderParamsError
from backend.app.render import TEMPLATE_VERSION

# Versions are pinned in most tests so the expected key below stays meaningful when the
# app is versioned up or Manim is upgraded.
PINNED = {"app_version": "0.0.0", "manim_version": "0.22.1", "template_version": "1"}
DEFAULTS = {"text": "Hello World"}


def key_for(**overrides: object) -> str:
    return cache.cache_key({**DEFAULTS, **overrides}, **PINNED)


# --------------------------------------------------------- the key is stable and correct


def test_key_is_lowercase_sha256_hex() -> None:
    key = key_for()
    assert len(key) == cache.KEY_HEX_LENGTH
    assert cache.KEY_PATTERN.fullmatch(key)


def test_same_params_give_the_same_key() -> None:
    """The acceptance rule for ROADMAP 2.2, stated three ways."""
    assert key_for() == key_for()
    assert cache.cache_key({"text": "Hi", "font_size": 96}, **PINNED) == cache.cache_key(
        {"font_size": 96, "text": "Hi"},
        **PINNED,
    )


def test_omitted_defaults_share_the_key_of_explicit_ones() -> None:
    """A request that leaves fields out is the same render as one that spells them out."""
    spelled_out = RenderParams().model_dump()
    assert cache.cache_key(DEFAULTS, **PINNED) == cache.cache_key(spelled_out, **PINNED)


def test_a_validated_model_and_its_dump_give_the_same_key() -> None:
    params = RenderParams(text="Sample")
    assert cache.cache_key(params, **PINNED) == cache.cache_key(
        params.model_dump(),
        **PINNED,
    )


def test_key_does_not_depend_on_background_key_order() -> None:
    first = {"text": "Hi", "background": {"color": "#123456", "type": "solid"}}
    second = {"text": "Hi", "background": {"type": "solid", "color": "#123456"}}
    assert cache.cache_key(first, **PINNED) == cache.cache_key(second, **PINNED)


def test_known_key_does_not_change_silently() -> None:
    """A digest lock: changing the hashed document invalidates every user's cache.

    That is sometimes correct (a real template change), so it is allowed -- but only on
    purpose, next to a reason in the PR.
    """
    assert key_for() == "3965eaf8d07e2cada4fee863b590a050146b867a9f1ade275fc60c51f5628898"


def test_non_ascii_text_keys_stably_and_differently() -> None:
    """Indonesian input is ordinary here, and must not collide with or lose its own key."""
    assert key_for(text="Terima Kasih") == key_for(text="Terima Kasih")
    assert key_for(text="Terima Kasih") != key_for(text="Terima Kasih ")


def test_newlines_are_part_of_the_key() -> None:
    assert key_for(text="Two\nLines") != key_for(text="Two Lines")


def test_invalid_params_are_rejected_before_any_key_is_made() -> None:
    with pytest.raises(RenderParamsError) as excinfo:
        key_for(text="")
    assert excinfo.value.code == "text_empty"
    with pytest.raises(RenderParamsError):
        key_for(font_size=999)


def test_leaving_versions_out_uses_this_machine_values() -> None:
    """The defaults are reproducible: they are exactly what this build and renderer report."""
    assert cache.cache_key(DEFAULTS) == cache.cache_key(
        DEFAULTS,
        app_version=app_meta.app_version(),
        manim_version=cache.installed_manim_version(),
        template_version=TEMPLATE_VERSION,
    )


def test_unknown_manim_version_is_an_error_not_a_blank(monkeypatch) -> None:
    empty = cache.check_env.CheckResult(name="Manim CE", ok=False, detail="missing")
    monkeypatch.setattr(cache.check_env, "check_manim", lambda: empty)
    with pytest.raises(cache.CacheError) as excinfo:
        cache.cache_key(DEFAULTS, app_version="0.0.0", template_version="1")
    assert excinfo.value.code == "manim_missing"


# ------------------------------------------------------- every field changes the key


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text", "Goodbye World"),
        ("animation", "fade_in"),
        ("animation_duration", 3.0),
        ("hold_duration", 2.0),
        ("font", "Roboto"),
        ("font_size", 97),
        ("text_color", "#FFFFFE"),
        ("outline_color", "#010101"),
        ("outline_width", 1.0),
        ("background", {"type": "solid", "color": "#101010"}),
        ("quality", "hd"),
    ],
)
def test_each_parameter_change_gives_a_new_key(field: str, value: object) -> None:
    """The other half of the ROADMAP 2.2 acceptance rule."""
    assert key_for(**{field: value}) != key_for()


@pytest.mark.parametrize("field", ["app_version", "manim_version", "template_version"])
def test_each_version_change_gives_a_new_key(field: str) -> None:
    """SPEC.md section 6: versions are in the key, so an upgrade cannot serve old output."""
    changed = dict(PINNED)
    changed[field] = f"{changed[field]}-next"
    assert cache.cache_key(DEFAULTS, **changed) != key_for()  # type: ignore[arg-type]


def test_a_real_template_bump_invalidates_the_old_key() -> None:
    assert cache.cache_key(DEFAULTS, **PINNED) != cache.cache_key(
        DEFAULTS,
        **{**PINNED, "template_version": "2"},
    )


def test_cache_and_render_share_one_template_version() -> None:
    assert cache.TEMPLATE_VERSION == TEMPLATE_VERSION
    assert isinstance(TEMPLATE_VERSION, str) and TEMPLATE_VERSION


# ------------------------------------------------------------------ the file name


def test_file_name_is_key_dash_quality_dash_extension() -> None:
    key = key_for()
    assert cache.video_file_name(key, "preview") == f"{key}-preview.mp4"
    assert cache.video_file_name(key, "hd") == f"{key}-hd.mp4"
    assert cache.VIDEO_NAME_PATTERN.fullmatch(cache.video_file_name(key, "hd"))


def test_the_stem_is_the_file_name_without_the_extension() -> None:
    """render_video is handed a stem, so the queue needs the two to agree."""
    key = key_for()
    assert cache.video_stem(key, "hd") == f"{key}-hd"
    assert cache.video_file_name(key, "hd") == f"{cache.video_stem(key, 'hd')}.mp4"


def test_is_cached_wants_a_real_non_empty_file(tmp_path) -> None:
    missing = tmp_path / f"{key_for()}-preview.mp4"
    assert cache.is_cached(missing) is False
    missing.touch()
    assert cache.is_cached(missing) is False, "an empty leftover is not a video"
    missing.write_bytes(b"video")
    assert cache.is_cached(missing) is True
    assert cache.is_cached(tmp_path) is False, "a directory is not a video either"


def test_one_key_per_quality_so_a_preview_never_answers_an_hd_request() -> None:
    key = key_for()
    assert cache.video_file_name(key, "preview") != cache.video_file_name(key, "hd")


@pytest.mark.parametrize(
    "bad_key",
    ["", "g" * 64, "A" * 64, key_for()[:-1], f"../{key_for()}", f"{key_for()}/preview"],
)
def test_a_key_that_is_not_plain_hex_is_refused(bad_key: str) -> None:
    with pytest.raises(cache.CacheError) as excinfo:
        cache.video_file_name(bad_key, "preview")
    assert excinfo.value.code == "cache_key_invalid"


@pytest.mark.parametrize("bad_quality", ["", "4K", "Preview", "../hd", "hd.mp4"])
def test_a_quality_outside_the_presets_is_refused(bad_quality: str) -> None:
    with pytest.raises(cache.CacheError) as excinfo:
        cache.video_file_name(key_for(), bad_quality)
    assert excinfo.value.code == "quality_invalid"


def test_video_path_stays_inside_the_output_directory(tmp_path) -> None:
    key = key_for()
    directory = tmp_path / "videos"
    directory.mkdir()
    path = cache.video_path(directory, key, "preview")
    assert path.parent == directory.resolve()
    assert path.name == f"{key}-preview.mp4"
    assert path.is_relative_to(directory.resolve())


# -------------------------------------------------------------- lookup with find_cached


def test_no_hit_before_the_file_exists(tmp_path) -> None:
    assert cache.find_cached(tmp_path, DEFAULTS, **PINNED) is None


def test_hit_once_the_file_exists(tmp_path) -> None:
    key = key_for()
    stored = tmp_path / f"{key}-preview.mp4"
    stored.write_bytes(b"video")
    assert cache.find_cached(tmp_path, DEFAULTS, **PINNED) == stored.resolve()


def test_an_empty_leftover_is_not_a_hit(tmp_path) -> None:
    """A killed render can leave a zero-byte file; the user should get a real render."""
    key = key_for()
    (tmp_path / f"{key}-preview.mp4").touch()
    assert cache.find_cached(tmp_path, DEFAULTS, **PINNED) is None


def test_find_cached_uses_the_params_quality(tmp_path) -> None:
    key = key_for(quality="hd")
    stored = tmp_path / f"{key}-hd.mp4"
    stored.write_bytes(b"video")
    assert cache.find_cached(tmp_path, {**DEFAULTS, "quality": "hd"}, **PINNED) == stored.resolve()


def test_a_quality_mismatch_is_refused(tmp_path) -> None:
    with pytest.raises(cache.CacheError) as excinfo:
        cache.find_cached(tmp_path, DEFAULTS, "hd", **PINNED)
    assert excinfo.value.code == "quality_mismatch"
    assert excinfo.value.params == {"requested": "hd", "params": "preview"}


# ------------------------------------------------------------------------ cleanup


def test_seven_days_is_the_documented_window() -> None:
    assert cache.MAX_AGE_SECONDS == 7 * 24 * 60 * 60


def test_cleanup_removes_only_files_past_the_window(tmp_path) -> None:
    """Roadmap 2.2: delete files older than 7 days, keep everything younger."""
    now = 1_700_000_000.0
    old = tmp_path / f"{key_for()}-preview.mp4"
    old.write_bytes(b"stale")
    os.utime(old, (now - 8 * 24 * 3600, now - 8 * 24 * 3600))
    recent = tmp_path / f"{key_for(text='Newer')}-preview.mp4"
    recent.write_bytes(b"fresh")
    os.utime(recent, (now - 6 * 24 * 3600, now - 6 * 24 * 3600))

    removed = cache.cleanup_stale_videos(tmp_path, now=now)

    assert removed == [old]
    assert not old.exists()
    assert recent.is_file()


def test_cleanup_keeps_a_file_exactly_at_the_boundary(tmp_path) -> None:
    """ "Older than 7 days" means past the window, not at it."""
    now = 1_700_000_000.0
    boundary = tmp_path / f"{key_for()}-hd.mp4"
    boundary.write_bytes(b"video")
    os.utime(boundary, (now - cache.MAX_AGE_SECONDS, now - cache.MAX_AGE_SECONDS))
    assert cache.cleanup_stale_videos(tmp_path, now=now) == []
    assert boundary.is_file()


def test_cleanup_leaves_other_files_alone(tmp_path) -> None:
    """Render logs and unrelated files live in the same tree and are not the cache."""
    now = 1_700_000_000.0
    stale = now - 40 * 24 * 3600
    log_file = tmp_path / "logs" / "render.log"
    log_file.parent.mkdir()
    log_file.write_text("log", encoding="utf-8")
    os.utime(log_file, (stale, stale))
    neighbours = [
        tmp_path / "clear-me-not.txt",
        tmp_path / "not-a-key.mp4",
        tmp_path / f"{key_for().upper()}-preview.mp4",
        tmp_path / f"{key_for()}-preview.mp4.bak",
        tmp_path / f"{key_for()}-preview.mov",
    ]
    for path in neighbours:
        path.write_bytes(b"x")
        os.utime(path, (stale, stale))

    assert cache.cleanup_stale_videos(tmp_path, now=now) == []
    assert log_file.is_file()
    assert all(path.exists() for path in neighbours)


def test_cleanup_handles_a_missing_directory(tmp_path) -> None:
    assert cache.cleanup_stale_videos(tmp_path / "never-created") == []


def test_cleanup_survives_a_file_it_cannot_delete(tmp_path, monkeypatch) -> None:
    """A locked file is a reason to log, not a reason to refuse to start."""
    now = 1_700_000_000.0
    stuck = tmp_path / f"{key_for()}-preview.mp4"
    stuck.write_bytes(b"stale")
    os.utime(stuck, (now - 9 * 24 * 3600, now - 9 * 24 * 3600))
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self == stuck:
            raise PermissionError("in use by the player")
        real_unlink(self)

    monkeypatch.setattr(Path, "unlink", refuse)
    try:
        assert cache.cleanup_stale_videos(tmp_path, now=now) == []
    finally:
        monkeypatch.setattr(Path, "unlink", real_unlink)
    assert stuck.is_file()


def test_cleanup_removes_every_stale_video(tmp_path) -> None:
    now = 1_700_000_000.0
    stale_day = now - 10 * 24 * 3600
    keys = [key_for(text=f"Line {index}") for index in range(4)]
    for key in keys:
        path = tmp_path / f"{key}-preview.mp4"
        path.write_bytes(b"video")
        os.utime(path, (stale_day, stale_day))
    removed = cache.cleanup_stale_videos(tmp_path, now=now)
    assert len(removed) == 4
    assert not any((tmp_path / f"{key}-preview.mp4").exists() for key in keys)
