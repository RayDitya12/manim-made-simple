"""The one fixed Manim template (SPEC.md sections 5 and 7).

Parameters reach this file only as a JSON document whose path is stored in the
``MTS_PARAMS`` environment variable. Nothing from that document is ever
interpolated into source code or a command string: it is parsed, re-validated
here, and only then handed to Manim.

Two schema fields are validated but not acted on here:

* ``quality`` -- the caller turns the preset into ``-r`` / ``--fps`` flags
  (``backend/app/render.py``, ROADMAP 1.4).
* ``font`` -- a bundled whitelist id, registered with Pango by
  :func:`register_bundled_font` before the text is built (ROADMAP 1.5). An omitted
  or ``null`` font still means the system default face, which only hand-written
  parameter files can ask for.

Run it by hand with a hand-written parameters file::

    MTS_PARAMS=params.json manim render -r 854,480 --fps 15 text_scene.py MmsTextScene
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import manimpango
from manim import *

PARAMS_ENV_VAR = "MTS_PARAMS"
FONTS_DIR = Path(__file__).resolve().parents[1] / "fonts"
FONT_FILE_SUFFIX = ".ttf"

ANIMATIONS: tuple[str, ...] = ("write", "fade_in", "typewriter")
QUALITIES: tuple[str, ...] = ("preview", "hd")
BUNDLED_FONTS: tuple[str, ...] = (
    "Inter",
    "Poppins",
    "Montserrat",
    "Roboto",
    "Playfair Display",
    "Bebas Neue",
    "Fira Code",
)
SOLID = "solid"
DEFERRED_BACKGROUNDS: tuple[str, ...] = ("transparent",)  # ROADMAP 4.1

TEXT_MAX_CHARS = 100
TEXT_MAX_LINES = 3
FONT_NAME_MAX_CHARS = 64
ANIMATION_DURATION_RANGE: tuple[float, float] = (0.5, 10.0)
HOLD_DURATION_RANGE: tuple[float, float] = (0.0, 10.0)
TOTAL_DURATION_MAX = 20.0
FONT_SIZE_RANGE: tuple[int, int] = (24, 200)
OUTLINE_WIDTH_RANGE: tuple[float, float] = (0.0, 10.0)
HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULTS: dict[str, Any] = {
    "text": "Hello World",
    "animation": "write",
    "animation_duration": 2.0,
    "hold_duration": 1.0,
    "font": "",
    "font_size": 96,
    "text_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 0.0,
    "background": {"type": SOLID, "color": "#000000"},
    "quality": "preview",
}


class ParamsError(ValueError):
    """A parameter is missing, malformed, or outside the SPEC.md section 5 limits."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _validated_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ParamsError("text_invalid", "text must be a string.")
    if not value.strip():
        raise ParamsError("text_empty", "text must contain at least one non-space character.")
    if len(value) > TEXT_MAX_CHARS:
        raise ParamsError(
            "text_too_long",
            f"text must be {TEXT_MAX_CHARS} characters or fewer, got {len(value)}.",
        )
    line_count = len(value.split("\n"))
    if line_count > TEXT_MAX_LINES:
        raise ParamsError(
            "too_many_lines",
            f"text must be {TEXT_MAX_LINES} lines or fewer, got {line_count}.",
        )
    for char in value:
        if char != "\n" and unicodedata.category(char) == "Cc":
            raise ParamsError("text_control_character", "text may not contain control characters.")
    return value


def _validated_choice(name: str, value: Any, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ParamsError(
            f"{name}_invalid",
            f"{name} must be one of {', '.join(choices)}, got {value!r}.",
        )
    return value


def _validated_number(name: str, value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParamsError(f"{name}_invalid", f"{name} must be a number.")
    if not low <= float(value) <= high:
        raise ParamsError(
            f"{name}_out_of_range",
            f"{name} must be between {low} and {high}, got {value}.",
        )
    return float(value)


def _validated_int(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParamsError(f"{name}_invalid", f"{name} must be a whole number.")
    if not low <= value <= high:
        raise ParamsError(
            f"{name}_out_of_range",
            f"{name} must be between {low} and {high}, got {value}.",
        )
    return value


def _validated_color(name: str, value: Any) -> str:
    if not isinstance(value, str) or not HEX_COLOR_PATTERN.match(value):
        raise ParamsError(
            f"{name}_invalid",
            f"{name} must be a hex color like #1A2B3C, got {value!r}.",
        )
    return value


def _validated_font(value: Any) -> str:
    """Return the font name. ``""`` (the default) and ``null`` mean the system face."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ParamsError("font_invalid", "font must be a non-empty font name or null.")
    if len(value) > FONT_NAME_MAX_CHARS:
        raise ParamsError(
            "font_invalid",
            f"font must be {FONT_NAME_MAX_CHARS} characters or fewer.",
        )
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ParamsError("font_invalid", "font may not contain control characters.")
    return value


def _validated_background(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ParamsError(
            "background_invalid",
            'background must be an object like {"type": "solid", "color": "#000000"}.',
        )
    kind = value.get("type", SOLID)
    if kind in DEFERRED_BACKGROUNDS:
        raise ParamsError(
            "background_type_unsupported",
            f"{kind} backgrounds are not supported yet; use a solid color.",
        )
    if kind != SOLID:
        raise ParamsError(
            "background_invalid",
            f'background type must be "{SOLID}", got {kind!r}.',
        )
    unknown = sorted(set(value) - {"type", "color"})
    if unknown:
        raise ParamsError(
            "params_field_unknown",
            f"unknown background fields: {', '.join(unknown)}.",
        )
    color = _validated_color("background_color", value.get("color", "#000000"))
    return {"type": SOLID, "color": color}


def _field(raw: dict[str, Any], name: str) -> Any:
    return raw[name] if name in raw else DEFAULTS[name]


def validate_params(raw: Any) -> dict[str, Any]:
    """Return a complete, in-range parameter set built from a parsed JSON object."""
    if not isinstance(raw, dict):
        raise ParamsError("params_not_object", "the parameters file must contain a JSON object.")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ParamsError("params_field_unknown", f"unknown parameters: {', '.join(unknown)}.")

    params: dict[str, Any] = dict(DEFAULTS)
    params["text"] = _validated_text(_field(raw, "text"))
    params["animation"] = _validated_choice("animation", _field(raw, "animation"), ANIMATIONS)
    params["animation_duration"] = _validated_number(
        "animation_duration",
        _field(raw, "animation_duration"),
        *ANIMATION_DURATION_RANGE,
    )
    params["hold_duration"] = _validated_number(
        "hold_duration",
        _field(raw, "hold_duration"),
        *HOLD_DURATION_RANGE,
    )
    total = params["animation_duration"] + params["hold_duration"]
    if total > TOTAL_DURATION_MAX:
        raise ParamsError(
            "total_duration_too_long",
            f"animation plus hold must be {TOTAL_DURATION_MAX} seconds or fewer, got {total}.",
        )
    params["font"] = _validated_font(_field(raw, "font"))
    params["font_size"] = _validated_int("font_size", _field(raw, "font_size"), *FONT_SIZE_RANGE)
    params["text_color"] = _validated_color("text_color", _field(raw, "text_color"))
    params["outline_color"] = _validated_color("outline_color", _field(raw, "outline_color"))
    params["outline_width"] = _validated_number(
        "outline_width",
        _field(raw, "outline_width"),
        *OUTLINE_WIDTH_RANGE,
    )
    params["background"] = _validated_background(_field(raw, "background"))
    params["quality"] = _validated_choice("quality", _field(raw, "quality"), QUALITIES)
    return params


def params_file_from_env() -> Path:
    """Return the parameters path named by ``MTS_PARAMS``."""
    raw_path = os.environ.get(PARAMS_ENV_VAR, "").strip()
    if not raw_path:
        raise ParamsError("params_file_missing", f"{PARAMS_ENV_VAR} names no parameters file.")
    return Path(raw_path)


def load_params(path: Path) -> dict[str, Any]:
    """Read, parse, and re-validate the parameters document at ``path``."""
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise ParamsError(
            "params_file_unreadable",
            f"the parameters file could not be read: {reason}.",
        ) from exc
    try:
        raw = json.loads(contents)
    except json.JSONDecodeError as exc:
        raise ParamsError(
            "params_not_json",
            f"parameters file is not valid JSON on line {exc.lineno}.",
        ) from exc
    return validate_params(raw)


def font_file_for(font_id: str) -> Path:
    """Return the bundled file for a whitelist font id."""
    return FONTS_DIR / f"{font_id.replace(' ', '')}{FONT_FILE_SUFFIX}"


def register_bundled_font(font_id: str) -> None:
    """Make a bundled font visible to Pango (SPEC.md section 9).

    A name outside the whitelist is left alone: only a hand-written parameters file
    can ask for one, and then it means a font already installed on the system.
    """
    if not font_id or font_id not in BUNDLED_FONTS:
        return
    path = font_file_for(font_id)
    if not path.is_file():
        raise ParamsError("font_file_missing", f"no bundled font file named {path.name}.")
    if not manimpango.register_font(str(path)):
        raise ParamsError("font_registration_failed", f"{path.name} could not be loaded.")


def build_text(params: dict[str, Any]) -> Text:
    """Build the text mobject, with the outline drawn behind the fill."""
    text = Text(
        text=params["text"],
        font_size=params["font_size"],
        color=ManimColor(params["text_color"]),
        font=params["font"],
        stroke_width=0,
    )
    if params["outline_width"] > 0:
        text.set_stroke(
            color=ManimColor(params["outline_color"]),
            width=params["outline_width"],
            background=True,
        )
    return text


def build_animation(text: Text, params: dict[str, Any]) -> Animation:
    """Map the SPEC.md section 5 animation ids onto their Manim animations."""
    run_time = params["animation_duration"]
    if params["animation"] == "write":
        return Write(text, run_time=run_time)
    if params["animation"] == "fade_in":
        return FadeIn(text, run_time=run_time)
    if params["animation"] == "typewriter":
        return AddTextLetterByLetter(text, run_time=run_time)
    raise ParamsError("animation_invalid", f"unsupported animation {params['animation']!r}.")


class MmsTextScene(Scene):
    """Render one animated-text clip from the parameters file named by ``MTS_PARAMS``."""

    def setup(self) -> None:
        self.params: dict[str, Any] = load_params(params_file_from_env())
        register_bundled_font(self.params["font"])
        background_color = ManimColor(self.params["background"]["color"])
        self.camera.background_color = background_color
        config.background_color = background_color

    def construct(self) -> None:
        text = build_text(self.params)
        self.play(build_animation(text, self.params))
        hold_duration = self.params["hold_duration"]
        if hold_duration > 0:
            self.wait(hold_duration)
