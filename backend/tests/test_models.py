"""Tests for backend/app/models.py (ROADMAP task 1.3).

Every rule in SPEC.md section 5 gets a boundary on each side, and every rejection
is checked by its stable error code (SPEC.md section 6), not by message wording.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_MODELS_PATH = _ROOT / "backend" / "app" / "models.py"
_SCENE_PATH = _ROOT / "backend" / "scenes" / "text_scene.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def models() -> Any:
    return _load("mms_models", _MODELS_PATH)


@pytest.fixture(scope="module")
def scene() -> Any:
    return _load("text_scene", _SCENE_PATH)


def _error_code(models: Any, raw: dict[str, Any]) -> str:
    with pytest.raises(models.RenderParamsError) as excinfo:
        models.parse_params(raw)
    return excinfo.value.code


# ------------------------------------------------------------------- defaults


def test_no_input_gives_the_spec_defaults(models: Any) -> None:
    params = models.parse_params({})
    assert params.text == "Hello World"
    assert params.animation == "write"
    assert params.animation_duration == 2.0
    assert params.hold_duration == 1.0
    assert params.font == "Inter"
    assert params.font_size == 96
    assert params.text_color == "#FFFFFF"
    assert params.outline_color == "#000000"
    assert params.outline_width == 0.0
    assert params.background.type == "solid"
    assert params.background.color == "#000000"
    assert params.quality == "preview"


def test_the_model_holds_exactly_the_spec_fields(models: Any) -> None:
    dumped = models.parse_params({}).model_dump()
    assert set(dumped) == {
        "text",
        "animation",
        "animation_duration",
        "hold_duration",
        "font",
        "font_size",
        "text_color",
        "outline_color",
        "outline_width",
        "background",
        "quality",
    }


def test_the_validated_result_is_plain_json(models: Any) -> None:
    dumped = models.parse_params({"text": "Hai", "hold_duration": 3}).model_dump()
    assert json.loads(json.dumps(dumped, sort_keys=True)) == dumped


def test_options_lists_are_derived_from_the_models(models: Any) -> None:
    assert models.ANIMATIONS == ("write", "fade_in", "typewriter")
    assert models.QUALITIES == ("preview", "hd")
    assert models.FONTS[0] == "Inter"
    assert set(models.NUMERIC_RANGES) == {
        "animation_duration",
        "hold_duration",
        "font_size",
        "outline_width",
    }


def test_input_is_not_mutated(models: Any) -> None:
    raw: dict[str, Any] = {"text": "Hi", "animation": "fade_in"}
    models.parse_params(raw)
    assert raw == {"text": "Hi", "animation": "fade_in"}


def test_validated_params_are_read_only(models: Any) -> None:
    params = models.parse_params({})
    with pytest.raises(models.ValidationError, match="frozen"):
        params.text = "changed"  # type: ignore[misc]


# ----------------------------------------------------------------- boundaries


@pytest.mark.parametrize("animation", ["write", "fade_in", "typewriter"])
@pytest.mark.parametrize("quality", ["preview", "hd"])
def test_every_allowed_choice_is_accepted(models: Any, animation: str, quality: str) -> None:
    for font in models.FONTS:
        params = models.parse_params({"animation": animation, "quality": quality, "font": font})
        assert (params.animation, params.quality, params.font) == (animation, quality, font)


@pytest.mark.parametrize("value", [0.5, 2, 5.25, 10, 10.0])
def test_animation_duration_boundaries(models: Any, value: float) -> None:
    assert models.parse_params({"animation_duration": value}).animation_duration == float(value)


@pytest.mark.parametrize("value", [0, 1, 10, 10.0])
def test_hold_duration_boundaries(models: Any, value: float) -> None:
    assert models.parse_params({"hold_duration": value}).hold_duration == float(value)


@pytest.mark.parametrize("value", [24, 96, 200])
def test_font_size_boundaries(models: Any, value: int) -> None:
    assert models.parse_params({"font_size": value}).font_size == value


@pytest.mark.parametrize("value", [0, 4.5, 10])
def test_outline_width_boundaries(models: Any, value: float) -> None:
    assert models.parse_params({"outline_width": value}).outline_width == float(value)


@pytest.mark.parametrize("value", ["a", "a" * 100, "one\ntwo\nthree", "  padded  "])
def test_text_boundaries(models: Any, value: str) -> None:
    assert models.parse_params({"text": value}).text == value


@pytest.mark.parametrize("value", ["#FFFFFF", "#000000", "#aB3cF0", "#123456"])
def test_hex_colors_are_accepted(models: Any, value: str) -> None:
    params = models.parse_params({"text_color": value, "outline_color": value})
    assert params.text_color == value
    assert params.outline_color == value


def test_twenty_second_total_is_still_allowed(models: Any) -> None:
    """SPEC.md section 5 says the total must not exceed 20 seconds, so 20 is legal."""
    params = models.parse_params({"animation_duration": 10, "hold_duration": 10})
    assert params.animation_duration + params.hold_duration == 20.0


def test_total_duration_cap_is_enforced(models: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The two per-field caps (10 s each) already keep the sum within 20 s, so the
    # total check is only reachable once the limit is lowered. Prove it is wired up.
    monkeypatch.setattr(models, "TOTAL_DURATION_MAX", 5.0)
    assert _error_code(models, {"animation_duration": 4, "hold_duration": 4}) == (
        "total_duration_too_long"
    )


# ----------------------------------------------------------------- rejections


@pytest.mark.parametrize(
    ("raw", "expected_code"),
    [
        # text
        ({"text": ""}, "text_empty"),
        ({"text": "   "}, "text_empty"),
        ({"text": "a" * 101}, "text_too_long"),
        ({"text": "one\ntwo\nthree\nfour"}, "too_many_lines"),
        ({"text": "tab\there"}, "text_control_character"),
        ({"text": "carriage\rreturn"}, "text_control_character"),
        ({"text": "bell\x07char"}, "text_control_character"),
        ({"text": 42}, "text_invalid"),
        ({"text": None}, "text_invalid"),
        # choices
        ({"animation": "spin"}, "animation_invalid"),
        ({"animation": "WRITE"}, "animation_invalid"),
        ({"animation": 5}, "animation_invalid"),
        ({"quality": "4k"}, "quality_invalid"),
        ({"quality": "HD"}, "quality_invalid"),
        ({"font": "Comic Sans MS"}, "font_invalid"),
        ({"font": "inter"}, "font_invalid"),
        ({"font": ""}, "font_invalid"),
        ({"font": None}, "font_invalid"),
        # numbers out of range
        ({"animation_duration": 0.4}, "animation_duration_out_of_range"),
        ({"animation_duration": 10.1}, "animation_duration_out_of_range"),
        ({"hold_duration": -0.1}, "hold_duration_out_of_range"),
        ({"hold_duration": 10.1}, "hold_duration_out_of_range"),
        ({"font_size": 23}, "font_size_out_of_range"),
        ({"font_size": 201}, "font_size_out_of_range"),
        ({"outline_width": -1}, "outline_width_out_of_range"),
        ({"outline_width": 10.1}, "outline_width_out_of_range"),
        # wrong number types
        ({"animation_duration": True}, "animation_duration_invalid"),
        ({"animation_duration": "2"}, "animation_duration_invalid"),
        ({"animation_duration": None}, "animation_duration_invalid"),
        ({"hold_duration": [1]}, "hold_duration_invalid"),
        ({"outline_width": "3"}, "outline_width_invalid"),
        ({"font_size": 96.0}, "font_size_invalid"),
        ({"font_size": 96.5}, "font_size_invalid"),
        ({"font_size": "96"}, "font_size_invalid"),
        ({"font_size": True}, "font_size_invalid"),
        ({"font_size": None}, "font_size_invalid"),
        # colors
        ({"text_color": "red"}, "text_color_invalid"),
        ({"text_color": "#FFFFF"}, "text_color_invalid"),
        ({"text_color": "#FFFFFFF"}, "text_color_invalid"),
        ({"text_color": "FFFFFF"}, "text_color_invalid"),
        ({"text_color": "#GGHHII"}, "text_color_invalid"),
        ({"text_color": "# 111111"}, "text_color_invalid"),
        ({"text_color": 16711680}, "text_color_invalid"),
        ({"outline_color": "#GGHHII"}, "outline_color_invalid"),
        ({"outline_color": "transparent"}, "outline_color_invalid"),
        # background
        ({"background": "transparent"}, "background_invalid"),
        ({"background": {"type": "transparent"}}, "background_type_unsupported"),
        (
            {"background": {"type": "gradient", "colors": ["#000000", "#FFFFFF"]}},
            "background_invalid",
        ),
        ({"background": {"type": 5}}, "background_invalid"),
        ({"background": {"type": None}}, "background_invalid"),
        ({"background": {"type": "solid", "color": "#12"}}, "background_color_invalid"),
        ({"background": {"type": "solid", "color": "black"}}, "background_color_invalid"),
        ({"background": {"type": "solid", "extra": 1}}, "params_field_unknown"),
        ({"background": 12}, "background_invalid"),
        # structure
        ({"typo_field": 1}, "params_field_unknown"),
        ({"self": "import os; os.system('rm -rf /')"}, "params_field_unknown"),
    ],
)
def test_out_of_spec_input_is_rejected_with_a_stable_code(
    models: Any, raw: dict[str, Any], expected_code: str
) -> None:
    assert _error_code(models, raw) == expected_code


@pytest.mark.parametrize("raw", [None, 5, "text", ["a", "b"], True])
def test_a_payload_that_is_not_an_object_is_refused(models: Any, raw: Any) -> None:
    assert _error_code(models, raw) == "params_not_object"  # type: ignore[arg-type]


def test_the_first_broken_field_reports(models: Any) -> None:
    """Several bad fields still give one predictable code, in schema order."""
    assert _error_code(models, {"text": "", "font_size": 500, "quality": "4k"}) == "text_empty"
    assert _error_code(models, {"font_size": 500, "quality": "4k"}) == "font_size_out_of_range"


# ------------------------------------------------------------- error payloads


def test_error_carries_a_plain_sentence_and_its_limits(models: Any) -> None:
    with pytest.raises(models.RenderParamsError) as excinfo:
        models.parse_params({"font_size": 500})
    error = excinfo.value
    assert error.code == "font_size_out_of_range"
    assert error.message == "font size must be between 24 and 200."
    assert error.params == {"field": "font_size", "min": 24, "max": 200}
    assert "\n" not in error.message


def test_color_error_names_the_field_not_the_input(models: Any) -> None:
    secretish = "#zzzzzz"
    with pytest.raises(models.RenderParamsError) as excinfo:
        models.parse_params({"text_color": secretish})
    error = excinfo.value
    assert error.code == "text_color_invalid"
    assert secretish not in error.message


def test_unknown_field_names_the_key(models: Any) -> None:
    with pytest.raises(models.RenderParamsError) as excinfo:
        models.parse_params({"colour": "#FFFFFF"})
    assert excinfo.value.params == {"field": "colour"}


# ----------------------------------------------------- agreement with the scene


def test_validated_params_pass_the_template_validator(models: Any, scene: Any) -> None:
    """The two validation layers must agree, or a valid request fails at render."""
    dumped = models.parse_params(
        {
            "text": "Two\nlines",
            "animation": "typewriter",
            "animation_duration": 1.5,
            "hold_duration": 0.5,
            "font": "Poppins",
            "font_size": 64,
            "text_color": "#123456",
            "outline_color": "#fedcba",
            "outline_width": 4,
            "background": {"type": "solid", "color": "#010203"},
            "quality": "hd",
        }
    ).model_dump()
    assert scene.validate_params(dumped) == dumped


@pytest.mark.parametrize("animation", ["write", "fade_in", "typewriter"])
def test_both_layers_share_the_animation_ids(models: Any, scene: Any, animation: str) -> None:
    assert models.parse_params({"animation": animation}).animation == animation
    assert scene.validate_params({"animation": animation})["animation"] == animation
