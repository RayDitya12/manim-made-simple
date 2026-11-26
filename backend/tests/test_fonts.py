"""Tests for the bundled fonts (ROADMAP task 1.5, SPEC.md section 9).

Part one checks the bundle itself: every whitelisted font id has a font file whose
internal family name matches, plus an OFL license text beside it.

Part two renders one frame per font through the real template and proves the frames
really differ, which is the only evidence that registration worked on this OS. On
Windows, registering the variable Montserrat and Fira Code files renders their
Regular named instance rather than the Thin and Light instances those files default
to, so the last test pins that down instead of trusting it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import manimpango
import numpy as np
import pytest
from PIL import Image, ImageFont

from backend.app import models

_ROOT = Path(__file__).resolve().parents[2]
_FONTS_DIR = _ROOT / "backend" / "fonts"
_LICENSES_DIR = _FONTS_DIR / "LICENSES"
_SCENE_PATH = _ROOT / "backend" / "scenes" / "text_scene.py"
_SAMPLE_TEXT = "Hamburgefonstiv 0123"


@pytest.fixture(scope="module")
def scene() -> Any:
    spec = importlib.util.spec_from_file_location("text_scene", _SCENE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["text_scene"] = module
    spec.loader.exec_module(module)
    return module


def _license_text(font_id: str) -> str:
    return (_LICENSES_DIR / f"{font_id.replace(' ', '')}.OFL.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the bundle


def test_both_layers_list_the_same_fonts(scene: Any) -> None:
    assert scene.BUNDLED_FONTS == models.FONTS


@pytest.mark.parametrize("font_id", models.FONTS)
def test_every_whitelisted_font_is_bundled_with_its_license(scene: Any, font_id: str) -> None:
    assert scene.font_file_for(font_id).is_file(), f"missing file for {font_id}"
    license_text = _license_text(font_id)
    assert license_text.splitlines()[0].startswith("Copyright")
    assert "SIL Open Font License" in license_text
    assert "PERMISSION & CONDITIONS" in license_text


@pytest.mark.parametrize("font_id", models.FONTS)
def test_the_file_advertises_the_id_the_api_offers(font_id: str) -> None:
    path = _FONTS_DIR / f"{font_id.replace(' ', '')}.ttf"
    assert ImageFont.truetype(str(path), 32).getname()[0] == font_id


def test_no_font_file_on_disk_is_unlicensed() -> None:
    on_disk = {path.stem for path in _FONTS_DIR.glob("*.ttf")}
    assert on_disk == {font.replace(" ", "") for font in models.FONTS}
    for name in on_disk:
        assert (_LICENSES_DIR / f"{name}.OFL.txt").is_file(), f"{name} has no license text"


# ------------------------------------------------------------- registration


@pytest.mark.parametrize("font_id", models.FONTS)
def test_registering_makes_the_font_visible_to_pango(scene: Any, font_id: str) -> None:
    path = scene.font_file_for(font_id)
    assert manimpango.register_font(str(path)) is True
    try:
        assert font_id in manimpango.list_fonts(), f"{font_id} stayed invisible to Pango"
    finally:
        manimpango.unregister_font(str(path))


def test_names_outside_the_whitelist_are_left_to_the_system(scene: Any) -> None:
    scene.register_bundled_font("")
    scene.register_bundled_font("Arial")


def test_a_missing_bundled_file_is_reported(
    scene: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scene, "FONTS_DIR", tmp_path)
    with pytest.raises(scene.ParamsError) as excinfo:
        scene.register_bundled_font(models.FONTS[0])
    assert excinfo.value.code == "font_file_missing"


# --------------------------------------------------------------- one frame each


@pytest.fixture(scope="module")
def frames(tmp_path_factory: pytest.TempPathFactory, scene: Any) -> dict[str, np.ndarray]:
    """Render the last frame of the template once per bundled font."""
    work_dir = tmp_path_factory.mktemp("font-frames")
    params_path = work_dir / "params.json"
    rendered: dict[str, np.ndarray] = {}
    for font_id in models.FONTS:
        params_path.write_text(
            json.dumps(
                {
                    "text": _SAMPLE_TEXT,
                    "font": font_id,
                    "animation": "fade_in",
                    "animation_duration": 0.5,
                    "hold_duration": 0,
                    "font_size": 64,
                    "text_color": "#FFFFFF",
                    "background": {"type": "solid", "color": "#000000"},
                    "quality": "preview",
                }
            ),
            encoding="utf-8",
        )
        media_dir = work_dir / font_id.replace(" ", "")
        command = [
            sys.executable,
            "-m",
            "manim",
            "render",
            "-s",
            "--verbosity",
            "ERROR",
            "--media_dir",
            str(media_dir),
            "-o",
            "frame.png",
            str(_SCENE_PATH),
            "MmsTextScene",
        ]
        result = subprocess.run(
            command,
            cwd=work_dir,
            env={**os.environ, scene.PARAMS_ENV_VAR: str(params_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, f"{font_id} failed:\n{result.stdout}\n{result.stderr}"
        produced = [p for p in sorted(media_dir.rglob("frame.png")) if "partial" not in p.parts]
        assert produced, f"no png written for {font_id}"
        image = np.asarray(Image.open(produced[0]).convert("RGB"))
        rendered[font_id] = image.min(axis=2) > 128
    return rendered


@pytest.mark.parametrize("font_id", models.FONTS)
def test_each_font_draws_its_text(frames: dict[str, np.ndarray], font_id: str) -> None:
    assert int(frames[font_id].sum()) > 5_000, f"{font_id} rendered almost nothing"


def test_the_seven_fonts_render_seven_different_shapes(frames: dict[str, np.ndarray]) -> None:
    shapes = {_shape(mask) for mask in frames.values()}
    assert len(shapes) == len(models.FONTS), "two fonts produced the same outline"


def test_regular_weight_is_selected_for_the_variable_fonts(frames: dict[str, np.ndarray]) -> None:
    baseline = int(frames["Inter"].sum())
    for font_id in ("Montserrat", "Fira Code"):
        ratio = int(frames[font_id].sum()) / baseline
        assert ratio > 0.6, f"{font_id} ink is {ratio:.2f} of Inter, so it rendered thin/light"


def _shape(mask: np.ndarray) -> bytes:
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    cropped = mask[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]
    small = Image.fromarray((cropped * 255).astype(np.uint8)).resize((32, 32), Image.BILINEAR)
    return (np.asarray(small) > 96).tobytes()
