"""Tests for the fixed Manim template (ROADMAP task 1.2).

Part one checks the parameters rules from SPEC.md section 5 without rendering.
Part two runs the real Manim CLI and inspects the produced MP4 frames.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import av
import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCENE_PATH = _ROOT / "backend" / "scenes" / "text_scene.py"
_PREVIEW_SIZE = (854, 480)
_PREVIEW_FPS = 15


@pytest.fixture(scope="module")
def scene():
    spec = importlib.util.spec_from_file_location("text_scene", _SCENE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["text_scene"] = module
    spec.loader.exec_module(module)
    return module


def _codes(scene, raw: dict[str, Any]) -> str:
    """Validate ``raw`` and return the error code it raises."""
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.validate_params(raw)
    return excinfo.value.code


# ---------------------------------------------------------------- validation


def test_missing_fields_take_the_spec_defaults(scene) -> None:
    params = scene.validate_params({})
    assert params["text"] == "Hello World"
    assert params["animation"] == "write"
    assert params["animation_duration"] == 2.0
    assert params["hold_duration"] == 1.0
    assert params["font"] == ""
    assert params["font_size"] == 96
    assert params["text_color"] == "#FFFFFF"
    assert params["outline_color"] == "#000000"
    assert params["outline_width"] == 0.0
    assert params["background"] == {"type": "solid", "color": "#000000"}
    assert params["quality"] == "preview"


def test_output_is_a_complete_validated_copy(scene) -> None:
    raw = {"text": "Hi", "animation": "fade_in"}
    params = scene.validate_params(raw)
    assert set(params) == set(scene.DEFAULTS)
    assert raw == {"text": "Hi", "animation": "fade_in"}, "the input must not be mutated"


@pytest.mark.parametrize("animation", ["write", "fade_in", "typewriter"])
def test_all_three_animations_are_accepted(scene, animation: str) -> None:
    assert scene.validate_params({"animation": animation})["animation"] == animation


@pytest.mark.parametrize("value", [0.5, 5, 10])
def test_animation_duration_boundaries(scene, value: float) -> None:
    assert scene.validate_params({"animation_duration": value})["animation_duration"] == float(
        value
    )


@pytest.mark.parametrize("value", [0, 10])
def test_hold_duration_boundaries(scene, value: float) -> None:
    assert scene.validate_params({"hold_duration": value})["hold_duration"] == float(value)


@pytest.mark.parametrize("value", [24, 96, 200])
def test_font_size_boundaries(scene, value: int) -> None:
    assert scene.validate_params({"font_size": value})["font_size"] == value


@pytest.mark.parametrize("value", [0, 5, 10])
def test_outline_width_boundaries(scene, value: float) -> None:
    assert scene.validate_params({"outline_width": value})["outline_width"] == float(value)


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        ({"text": "a" * 101}, "text_too_long"),
        ({"text": ""}, "text_empty"),
        ({"text": "   "}, "text_empty"),
        ({"text": "one\ntwo\nthree\nfour"}, "too_many_lines"),
        ({"text": "tab\there"}, "text_control_character"),
        ({"text": "carriage\rreturn"}, "text_control_character"),
        ({"text": "bell\x07char"}, "text_control_character"),
        ({"text": 42}, "text_invalid"),
        ({"animation": "spin"}, "animation_invalid"),
        ({"animation_duration": 0.4}, "animation_duration_out_of_range"),
        ({"animation_duration": 10.1}, "animation_duration_out_of_range"),
        ({"animation_duration": True}, "animation_duration_invalid"),
        ({"animation_duration": "2"}, "animation_duration_invalid"),
        ({"hold_duration": -0.1}, "hold_duration_out_of_range"),
        ({"hold_duration": 10.1}, "hold_duration_out_of_range"),
        ({"animation_duration": 10.0001, "hold_duration": 0}, "animation_duration_out_of_range"),
        ({"font": 12}, "font_invalid"),
        ({"font": "Bad\x00Font"}, "font_invalid"),
        ({"font_size": 23}, "font_size_out_of_range"),
        ({"font_size": 201}, "font_size_out_of_range"),
        ({"font_size": 96.0}, "font_size_invalid"),
        ({"font_size": True}, "font_size_invalid"),
        ({"text_color": "red"}, "text_color_invalid"),
        ({"text_color": "#FFFFF"}, "text_color_invalid"),
        ({"text_color": "#FFFFFFF"}, "text_color_invalid"),
        ({"text_color": "FFFFFF"}, "text_color_invalid"),
        ({"outline_color": "#GGHHII"}, "outline_color_invalid"),
        ({"outline_width": 10.1}, "outline_width_out_of_range"),
        ({"outline_width": -1}, "outline_width_out_of_range"),
        ({"background": "transparent"}, "background_invalid"),
        ({"background": {"type": "transparent"}}, "background_type_unsupported"),
        (
            {"background": {"type": "gradient", "colors": ["#000000", "#FFFFFF"]}},
            "background_invalid",
        ),
        ({"background": {"type": "solid", "color": "#12"}}, "background_color_invalid"),
        ({"background": {"type": "solid", "extra": 1}}, "params_field_unknown"),
        ({"quality": "4k"}, "quality_invalid"),
        ({"typo_field": 1}, "params_field_unknown"),
    ],
)
def test_out_of_spec_input_is_rejected(scene, raw: dict[str, Any], expected_code: str) -> None:
    assert _codes(scene, raw) == expected_code


def test_empty_font_means_the_system_default(scene) -> None:
    assert scene.validate_params({"font": ""})["font"] == ""
    assert scene.validate_params({"font": None})["font"] == ""


def test_twenty_second_total_is_still_allowed(scene) -> None:
    """SPEC.md section 5: the total "must not exceed" 20 seconds, so 20 is legal."""
    params = scene.validate_params({"animation_duration": 10, "hold_duration": 10})
    assert params["animation_duration"] + params["hold_duration"] == 20.0


def test_total_duration_cap_rejects_when_the_individual_ranges_allow_it(scene, monkeypatch) -> None:
    # The two per-field caps (10 s each) already keep the sum within 20 s, so the
    # total check is only reachable once the limit is lowered. Prove it is wired up.
    monkeypatch.setattr(scene, "TOTAL_DURATION_MAX", 5.0)
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.validate_params({"animation_duration": 4, "hold_duration": 4})
    assert excinfo.value.code == "total_duration_too_long"


def test_lowercase_hex_colors_are_accepted(scene) -> None:
    params = scene.validate_params({"text_color": "#aB3cF0", "outline_color": "#000000"})
    assert params["text_color"] == "#aB3cF0"


def test_three_lines_are_accepted(scene) -> None:
    assert scene.validate_params({"text": "one\ntwo\nthree"})["text"] == "one\ntwo\nthree"


def test_root_must_be_an_object(scene) -> None:
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.validate_params(["not", "a", "dict"])
    assert excinfo.value.code == "params_not_object"


# ------------------------------------------------------------- params files


def test_env_var_names_the_file(scene, tmp_path: Path) -> None:
    params_path = tmp_path / "params.json"
    params_path.write_text(json.dumps({"text": "From file"}), encoding="utf-8")
    os.environ["MTS_PARAMS"] = str(params_path)
    try:
        assert scene.params_file_from_env() == params_path
        assert scene.load_params(scene.params_file_from_env())["text"] == "From file"
    finally:
        del os.environ["MTS_PARAMS"]


def test_missing_env_var(scene, monkeypatch) -> None:
    monkeypatch.delenv("MTS_PARAMS", raising=False)
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.params_file_from_env()
    assert excinfo.value.code == "params_file_missing"


def test_unreadable_file(scene, tmp_path: Path) -> None:
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.load_params(tmp_path / "nope.json")
    assert excinfo.value.code == "params_file_unreadable"


def test_file_that_is_not_json(scene, tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text('{"text": "oops"', encoding="utf-8")
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.load_params(broken)
    assert excinfo.value.code == "params_not_json"


def test_file_with_unknown_field_is_refused(scene, tmp_path: Path) -> None:
    sneaky = tmp_path / "sneaky.json"
    sneaky.write_text(
        '{"text": "hi", "self": "import os; os.system(\'rm -rf /\')"}', encoding="utf-8"
    )
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.load_params(sneaky)
    assert excinfo.value.code == "params_field_unknown"


# ------------------------------------------------------------------ renders


def _render(tmp_path: Path, params: dict[str, Any], name: str) -> Path:
    """Run the real Manim CLI on ``params`` and return the preview-quality MP4."""
    params_path = tmp_path / f"{name}.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")
    media_dir = tmp_path / f"media_{name}"
    env = {**os.environ, "MTS_PARAMS": str(params_path)}
    command = [
        sys.executable,
        "-m",
        "manim",
        "render",
        "-r",
        f"{_PREVIEW_SIZE[0]},{_PREVIEW_SIZE[1]}",
        "--fps",
        str(_PREVIEW_FPS),
        "--disable_caching",
        "--media_dir",
        str(media_dir),
        "-o",
        f"{name}.mp4",
        str(_SCENE_PATH),
        "MmsTextScene",
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"manim failed:\n{result.stdout}\n{result.stderr}"
    finals = [p for p in sorted(media_dir.rglob(f"{name}.mp4")) if "partial" not in p.parts]
    assert finals, f"no mp4 produced:\n{result.stdout}"
    return finals[0]


def _video_frames(video: Path) -> list[np.ndarray]:
    with av.open(str(video)) as container:
        return [
            frame.to_ndarray(format="rgb24")
            for frame in container.decode(container.streams.video[0])
        ]


def _rgb(hex_color: str) -> tuple[int, int, int]:
    digits = hex_color.lstrip("#")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _near(frame: np.ndarray, color: tuple[int, int, int], tolerance: int = 30) -> np.ndarray:
    delta = np.abs(frame.astype(int) - np.array(color, dtype=int))
    return (delta <= tolerance).all(axis=2)


def _count(frame: np.ndarray, color: tuple[int, int, int], tolerance: int = 30) -> int:
    return int(_near(frame, color, tolerance).sum())


@pytest.fixture(scope="module")
def render_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("renders")


@pytest.mark.parametrize("animation", ["write", "fade_in", "typewriter"])
def test_each_animation_renders_a_preview_mp4(render_dir: Path, animation: str) -> None:
    video = _render(
        render_dir,
        {
            "text": "Motion test",
            "animation": animation,
            "animation_duration": 1,
            "hold_duration": 0.4,
            "text_color": "#FFFFFF",
            "background": {"type": "solid", "color": "#000000"},
            "quality": "preview",
        },
        animation,
    )
    frames = _video_frames(video)
    assert len(frames) >= 15
    for frame in frames:
        assert frame.shape[1] == _PREVIEW_SIZE[0]
        assert frame.shape[0] == _PREVIEW_SIZE[1]
    assert _count(frames[-1], _rgb("#FFFFFF")) > 500, "the text must be fully visible at the end"
    half = len(frames) // 3
    assert _count(frames[half], _rgb("#FFFFFF")) < _count(frames[-1], _rgb("#FFFFFF")), (
        f"{animation} must reveal the text over time"
    )


def test_solid_background_color_is_rendered(render_dir: Path) -> None:
    video = _render(
        render_dir,
        {
            "text": "BG",
            "animation": "fade_in",
            "animation_duration": 0.5,
            "hold_duration": 0,
            "background": {"type": "solid", "color": "#1A2B3C"},
        },
        "background",
    )
    corners = _video_frames(video)[0]
    for x, y in ((2, 2), (2, 477), (851, 2), (851, 477)):
        assert _near(corners[y : y + 2, x : x + 2], _rgb("#1A2B3C"), tolerance=40).any(), (
            f"corner {x},{y}"
        )


def test_outline_is_drawn_behind_the_fill(render_dir: Path) -> None:
    white = _rgb("#FFFFFF")
    red = _rgb("#FF0000")
    shared = {
        "text": "Illl",
        "animation": "fade_in",
        "animation_duration": 0.5,
        "hold_duration": 0.1,
        "font_size": 120,
        "text_color": "#FFFFFF",
        "background": {"type": "solid", "color": "#000000"},
    }
    plain = _video_frames(_render(render_dir, {**shared, "outline_width": 0}, "plain"))[-1]
    outlined = _video_frames(
        _render(render_dir, {**shared, "outline_color": "#FF0000", "outline_width": 10}, "outlined")
    )[-1]

    assert _count(outlined, red) > 100, "the outline must actually be drawn"
    assert _count(plain, red) == 0, "outline_width 0 must draw no outline"

    # The test that matters for SPEC.md section 5: painting the stroke on top
    # would replace the fill pixels. Measured on this pair, background=True keeps
    # 97% of the plain fill pixels, background=False keeps 22%.
    plain_fill = _near(plain, white, 40)
    still_fill = plain_fill & _near(outlined, white, 40)
    ratio = int(still_fill.sum()) / max(int(plain_fill.sum()), 1)
    assert ratio >= 0.9, f"only {ratio:.1%} of the fill survives under the outline, so it is on top"


def test_bad_params_stop_the_render(render_dir: Path) -> None:
    params_path = render_dir / "bad_params.json"
    params_path.write_text(json.dumps({"text": "x" * 120}), encoding="utf-8")
    env = {**os.environ, "MTS_PARAMS": str(params_path)}
    command = [
        sys.executable,
        "-m",
        "manim",
        "render",
        "-r",
        "854,480",
        "--fps",
        "15",
        "--media_dir",
        str(render_dir / "media_bad"),
        str(_SCENE_PATH),
        "MmsTextScene",
    ]
    result = subprocess.run(
        command, cwd=render_dir, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode != 0
    assert "text_too_long" in result.stdout + result.stderr
