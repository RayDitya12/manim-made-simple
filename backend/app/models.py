"""Render-parameter models and validation (SPEC.md section 5).

This module is the front door for everything that arrives over the API. The
fixed Manim template in ``backend/scenes/text_scene.py`` re-validates the same
document on its own (SPEC.md section 7), so the two layers agree on the rules
and on the names of the error codes.

The codes carried by :class:`RenderParamsError` are the stable codes SPEC.md
section 6 promises the UI: the translation lives in the locale files, the
English ``message`` is the fallback, and ``params`` holds the numbers a
translated sentence needs (limits, counts). No raw user input is put in a
message.

``font`` takes ids from the SPEC.md section 9 whitelist, which is the set of fonts
bundled in ``backend/fonts/`` with their OFL texts. This layer is stricter than the
template: ``font`` has to be a whitelisted id, so ``""`` and ``null`` are rejected
rather than read as "whatever the system default is".
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

Animation = Literal["write", "fade_in", "typewriter"]
Quality = Literal["preview", "hd"]
FontId = Literal[
    "Inter",
    "Poppins",
    "Montserrat",
    "Roboto",
    "Playfair Display",
    "Bebas Neue",
    "Fira Code",
]

ANIMATIONS: tuple[str, ...] = get_args(Animation)
QUALITIES: tuple[str, ...] = get_args(Quality)
FONTS: tuple[str, ...] = get_args(FontId)

TEXT_MAX_CHARS = 100
TEXT_MAX_LINES = 3
ANIMATION_DURATION_RANGE: tuple[float, float] = (0.5, 10.0)
HOLD_DURATION_RANGE: tuple[float, float] = (0.0, 10.0)
FONT_SIZE_RANGE: tuple[int, int] = (24, 200)
OUTLINE_WIDTH_RANGE: tuple[float, float] = (0.0, 10.0)
TOTAL_DURATION_MAX = 20.0
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"

SOLID = "solid"
TRANSPARENT = "transparent"

TEXT_DEFAULT = "Hello World"
ANIMATION_DEFAULT = "write"
ANIMATION_DURATION_DEFAULT = 2.0
HOLD_DURATION_DEFAULT = 1.0
FONT_SIZE_DEFAULT = 96
TEXT_COLOR_DEFAULT = "#FFFFFF"
OUTLINE_COLOR_DEFAULT = "#000000"
OUTLINE_WIDTH_DEFAULT = 0.0
BACKGROUND_COLOR_DEFAULT = "#000000"
QUALITY_DEFAULT = "preview"

NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "animation_duration": ANIMATION_DURATION_RANGE,
    "hold_duration": HOLD_DURATION_RANGE,
    "font_size": FONT_SIZE_RANGE,
    "outline_width": OUTLINE_WIDTH_RANGE,
}

HexColor = Annotated[str, StringConstraints(pattern=HEX_COLOR_PATTERN)]


class RenderParamsError(ValueError):
    """Input is outside SPEC.md section 5. Carries the stable error code."""

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.params: dict[str, Any] = dict(params or {})


def _number_validator(field: str) -> Callable[[Any], Any]:
    """Reject ``true`` and ``"2"``: JSON numbers only, as SPEC.md section 5 means."""

    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PydanticCustomError(
                f"{field}_invalid",
                f"{field.replace('_', ' ')} must be a number.",
                {"field": field},
            )
        return value

    return check


def _reject_transparent(value: Any) -> str:
    """SPEC.md section 5 makes a transparent background available from Phase 4."""
    if value == TRANSPARENT:
        raise PydanticCustomError(
            "background_type_unsupported",
            "transparent backgrounds are not supported yet, use a solid color.",
        )
    if value != SOLID:
        raise PydanticCustomError(
            "background_invalid",
            "background type must be solid.",
        )
    return str(value)


class Background(BaseModel):
    """The ``background`` object: ``{"type": "solid", "color": "#RRGGBB"}``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Annotated[str, BeforeValidator(_reject_transparent)] = SOLID
    color: HexColor = BACKGROUND_COLOR_DEFAULT


class RenderParams(BaseModel):
    """Validated render parameters, exactly the SPEC.md section 5 schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = TEXT_DEFAULT
    animation: Animation = ANIMATION_DEFAULT
    animation_duration: Annotated[
        float, BeforeValidator(_number_validator("animation_duration"))
    ] = Field(
        ANIMATION_DURATION_DEFAULT,
        ge=ANIMATION_DURATION_RANGE[0],
        le=ANIMATION_DURATION_RANGE[1],
    )
    hold_duration: Annotated[float, BeforeValidator(_number_validator("hold_duration"))] = Field(
        HOLD_DURATION_DEFAULT,
        ge=HOLD_DURATION_RANGE[0],
        le=HOLD_DURATION_RANGE[1],
    )
    font: FontId = FONTS[0]
    font_size: int = Field(
        FONT_SIZE_DEFAULT,
        ge=FONT_SIZE_RANGE[0],
        le=FONT_SIZE_RANGE[1],
        strict=True,
    )
    text_color: HexColor = TEXT_COLOR_DEFAULT
    outline_color: HexColor = OUTLINE_COLOR_DEFAULT
    outline_width: Annotated[float, BeforeValidator(_number_validator("outline_width"))] = Field(
        OUTLINE_WIDTH_DEFAULT,
        ge=OUTLINE_WIDTH_RANGE[0],
        le=OUTLINE_WIDTH_RANGE[1],
    )
    background: Background = Field(default_factory=Background)
    quality: Quality = QUALITY_DEFAULT

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError(
                "text_empty",
                "text must contain at least one non-space character.",
            )
        if len(value) > TEXT_MAX_CHARS:
            raise PydanticCustomError(
                "text_too_long",
                "text must be at most {max} characters.",
                {"max": TEXT_MAX_CHARS, "got": len(value)},
            )
        line_count = len(value.split("\n"))
        if line_count > TEXT_MAX_LINES:
            raise PydanticCustomError(
                "too_many_lines",
                "text must be at most {max} lines.",
                {"max": TEXT_MAX_LINES, "got": line_count},
            )
        if any(char != "\n" and unicodedata.category(char) == "Cc" for char in value):
            raise PydanticCustomError(
                "text_control_character",
                "text may not contain control characters.",
            )
        return value

    @model_validator(mode="after")
    def _check_total_duration(self) -> RenderParams:
        total = self.animation_duration + self.hold_duration
        if total > TOTAL_DURATION_MAX:
            raise PydanticCustomError(
                "total_duration_too_long",
                "animation plus hold must be at most {max} seconds.",
                {"max": TOTAL_DURATION_MAX, "got": total},
            )
        return self


# Error codes this module raises on purpose, as opposed to ones pydantic derives.
_CUSTOM_CODES = frozenset(
    {
        "text_empty",
        "text_too_long",
        "too_many_lines",
        "text_control_character",
        "animation_duration_invalid",
        "hold_duration_invalid",
        "outline_width_invalid",
        "background_invalid",
        "background_type_unsupported",
        "total_duration_too_long",
    }
)

_TYPE_MESSAGES = {
    "string_type": "must be a string.",
    "int_type": "must be a whole number.",
    "int_from_float": "must be a whole number.",
    "float_type": "must be a number.",
    "bool_type": "must be true or false.",
    "dict_type": "must be an object.",
    "model_type": "must be an object.",
    "model_attributes_type": "must be an object.",
    "string_pattern_mismatch": "must be a hex color like #1A2B3C.",
}


def _derived_error(err: dict[str, Any]) -> RenderParamsError:
    kind = str(err["type"])
    loc = tuple(str(part) for part in err["loc"])
    ctx = err.get("ctx") or {}

    if kind in _CUSTOM_CODES:
        return RenderParamsError(kind, str(err["msg"]), ctx)
    if kind == "extra_forbidden":
        field = loc[-1] if loc else ""
        return RenderParamsError(
            "params_field_unknown",
            f"unknown parameter: {field}.",
            {"field": field},
        )

    field = "_".join(loc)
    if kind in ("greater_than_equal", "less_than_equal"):
        low, high = NUMERIC_RANGES[field]
        return RenderParamsError(
            f"{field}_out_of_range",
            f"{field.replace('_', ' ')} must be between {low} and {high}.",
            {"field": field, "min": low, "max": high},
        )
    if kind == "literal_error":
        expected = ctx.get("expected", "one of the allowed values")
        return RenderParamsError(
            f"{field}_invalid",
            f"{field.replace('_', ' ')} must be one of {expected}.",
            {"field": field},
        )
    if kind in _TYPE_MESSAGES:
        return RenderParamsError(
            f"{field}_invalid",
            f"{field.replace('_', ' ')} {_TYPE_MESSAGES[kind]}",
            {"field": field},
        )
    return RenderParamsError("params_invalid", str(err["msg"]), {"field": field})


def parse_params(raw: Any) -> RenderParams:
    """Validate an external payload and return the typed parameters.

    Raises :class:`RenderParamsError` with the first rule the input breaks.
    """
    if not isinstance(raw, dict):
        raise RenderParamsError("params_not_object", "the parameters must be a JSON object.")
    try:
        return RenderParams.model_validate(raw)
    except ValidationError as exc:
        raise _derived_error(exc.errors()[0]) from exc
