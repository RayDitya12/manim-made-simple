"""Tests for backend/app/render.py (ROADMAP task 1.4).

Three kinds of check, in this order:

* the parameter and command plumbing, with the child process stubbed out;
* the subprocess behaviour (timeout, failure, missing output) against a stand-in
  for the Manim CLI, so the failure paths are exercised without waiting a minute;
* one real render of a one-second preview clip through the actual Manim binary.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import av
import pytest

from backend.app import render
from backend.app.models import RenderParamsError
from backend.app.render import _find_output, render_video

_ROOT = Path(__file__).resolve().parents[2]
_FAKE_MANIM = '''"""Stand-in for the manim CLI, driven by FAKE_BEHAVIOUR."""

import json
import os
import sys
import time
from pathlib import Path


def flag(name, default):
    argv = sys.argv
    return argv[argv.index(name) + 1] if name in argv else default


media_dir = Path(flag("--media_dir", "."))
out_name = flag("-o", "clip.mp4")
params_path = os.environ.get("MTS_PARAMS", "")
params = {}
if params_path and Path(params_path).is_file():
    params = json.loads(Path(params_path).read_text(encoding="utf-8"))

behaviour = os.environ.get("FAKE_BEHAVIOUR", "produce")

if behaviour == "hang":
    heartbeat = Path(os.environ["FAKE_HEARTBEAT"])
    while True:
        heartbeat.write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(0.1)
if behaviour == "fail":
    print("fake manim: could not render the scene", file=sys.stderr)
    raise SystemExit(3)
if behaviour == "nothing":
    print("fake manim: finished without writing a video")
    raise SystemExit(0)

video_dir = media_dir / "videos" / "MmsTextScene" / "854_480_15"
video_dir.mkdir(parents=True, exist_ok=True)
(video_dir / out_name).write_text(
    json.dumps({"text": params.get("text"), "quality": params.get("quality")}),
    encoding="utf-8",
)
partial_dir = media_dir / "videos" / "MmsTextScene" / "partial_movie_files"
partial_dir.mkdir(parents=True, exist_ok=True)
(partial_dir / out_name).write_text("partial segment", encoding="utf-8")
raise SystemExit(0)
'''


@pytest.fixture
def fake_manim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Manim at a script whose behaviour is chosen through environment vars."""
    script = tmp_path / "fake_manim.py"
    script.write_text(_FAKE_MANIM, encoding="utf-8")
    monkeypatch.setattr(render, "MANIM_COMMAND", (sys.executable, str(script)))
    return tmp_path


def _preset(timeout: float = 30.0) -> render.QualityPreset:
    return render.QualityPreset(width=854, height=480, fps=15, timeout_seconds=timeout)


def _lit_pixels(frame: Any) -> int:
    """Count pixels that are bright in all three channels, i.e. the white text."""
    return int((frame.min(axis=2) > 200).sum())


# --------------------------------------------------------- validation first


def test_invalid_params_never_start_a_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render.subprocess, "Popen", _refuse_to_spawn)
    with pytest.raises(RenderParamsError) as excinfo:
        render_video({"text": "x" * 101}, tmp_path / "out", "clip")
    assert excinfo.value.code == "text_too_long"
    assert not (tmp_path / "out").exists()


def _refuse_to_spawn(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("render.py must validate before it spawns anything")


@pytest.mark.parametrize(
    "file_stem",
    ["", "..", "../evil", "a/b", "a\\b", "sub dir/clip", ".hidden", "x" * 81, "./clip"],
)
def test_output_names_cannot_escape_the_output_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_stem: str
) -> None:
    monkeypatch.setattr(render.subprocess, "Popen", _refuse_to_spawn)
    with pytest.raises(render.RenderError) as excinfo:
        render_video({}, tmp_path / "out", file_stem)
    assert excinfo.value.code == "file_name_invalid"


# ------------------------------------------------- how the child is spawned


def test_child_gets_no_shell_and_none_of_the_user_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SPEC.md section 7: parameters travel in the JSON file, never in argv."""
    seen: dict[str, Any] = {}

    class StubProcess:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            seen["command"] = list(command)
            seen["kwargs"] = kwargs

        def communicate(self, timeout: float | None = None) -> tuple[str, None]:
            params_path = Path(seen["kwargs"]["env"][render.PARAMS_ENV_VAR])
            seen["params_json"] = params_path.read_text(encoding="utf-8")
            return ("stub manim output", None)

    monkeypatch.setattr(render.subprocess, "Popen", StubProcess)
    hostile = "Robert'); DROP TABLE x; rm -rf / `id` $(whoami)"
    with pytest.raises(render.RenderError) as excinfo:
        render_video(
            {"text": hostile, "animation": "typewriter", "font": "Fira Code"},
            tmp_path / "out",
            "stub",
        )
    assert excinfo.value.code == "render_output_missing"

    assert not seen["kwargs"].get("shell")
    assert all("rm -rf" not in part and "DROP TABLE" not in part for part in seen["command"])
    assert seen["command"][0] == sys.executable
    assert seen["params_json"].find("DROP TABLE") >= 0, "the text belongs in the JSON file"
    assert json.loads(seen["params_json"])["animation"] == "typewriter"


# ------------------------------------------------------------- command build


def test_presets_match_the_spec() -> None:
    assert render.QUALITY_PRESETS["preview"] == render.QualityPreset(854, 480, 15, 60.0)
    assert render.QUALITY_PRESETS["hd"] == render.QualityPreset(1920, 1080, 60, 120.0)


def test_command_carries_the_quality_flags() -> None:
    command = render.build_manim_command(_preset(), Path("media"), "clip.mp4")
    assert command[: len(render.MANIM_COMMAND)] == list(render.MANIM_COMMAND)
    assert command[len(render.MANIM_COMMAND)] == "render"
    assert command[command.index("--media_dir") + 1] == "media"
    assert command[command.index("-r") + 1] == "854,480"
    assert command[command.index("--fps") + 1] == "15"
    assert command[command.index("-o") + 1] == "clip.mp4"
    assert command[-2:] == [str(render.SCENE_PATH), "MmsTextScene"]


def test_hd_command_uses_the_hd_preset() -> None:
    command = render.build_manim_command(render.QUALITY_PRESETS["hd"], Path("m"), "clip.mp4")
    assert command[command.index("-r") + 1] == "1920,1080"
    assert command[command.index("--fps") + 1] == "60"


def test_scene_template_is_where_render_expects_it() -> None:
    assert render.SCENE_PATH.is_file()
    assert render.PARAMS_ENV_VAR == "MTS_PARAMS"


# ------------------------------------------------------- subprocess plumbing


def test_params_reach_the_template_as_json(
    fake_manim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BEHAVIOUR", "produce")
    video = render_video(
        {"text": "Hello from JSON", "quality": "preview"}, fake_manim / "out", "clip-01"
    )
    assert video == fake_manim / "out" / "clip-01.mp4"
    assert json.loads(video.read_text(encoding="utf-8")) == {
        "text": "Hello from JSON",
        "quality": "preview",
    }


def test_partial_movie_files_are_not_returned(fake_manim: Path) -> None:
    media = fake_manim / "media"
    final = media / "videos" / "MmsTextScene" / "854_480_15"
    final.mkdir(parents=True)
    (final / "clip.mp4").write_bytes(b"real")
    partial = final / "partial_movie_files" / "hash"
    partial.mkdir(parents=True)
    (partial / "clip.mp4").write_bytes(b"segment")
    assert _find_output(media, "clip.mp4") == final / "clip.mp4"


def test_empty_output_file_is_not_a_result(fake_manim: Path) -> None:
    media = fake_manim / "media"
    final = media / "videos" / "MmsTextScene" / "854_480_15"
    final.mkdir(parents=True)
    (final / "clip.mp4").write_bytes(b"")
    assert _find_output(media, "clip.mp4") is None


def test_partial_directory_named_like_the_file_is_skipped(fake_manim: Path) -> None:
    media = fake_manim / "media"
    partial = media / "videos" / "partial_movie_files"
    partial.mkdir(parents=True)
    (partial / "clip.mp4").write_bytes(b"segment")
    assert _find_output(media, "clip.mp4") is None


def test_work_directory_is_cleaned_up(fake_manim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_BEHAVIOUR", "produce")
    before = _leftover_work_dirs()
    render_video({"text": "Clean"}, fake_manim / "out", "cleaned")
    assert _leftover_work_dirs() == before


def _leftover_work_dirs() -> set[str]:
    return {path.name for path in Path(tempfile.gettempdir()).glob("mms-render-*")}


def test_failing_render_reports_and_keeps_a_log(
    fake_manim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BEHAVIOUR", "fail")
    with pytest.raises(render.RenderError) as excinfo:
        render_video({"text": "Boom"}, fake_manim / "out", "boom")
    assert excinfo.value.code == "render_failed"
    assert "\n" not in excinfo.value.message
    log = (fake_manim / "out" / "logs" / "boom.log").read_text(encoding="utf-8")
    assert "could not render the scene" in log


def test_render_that_exits_zero_without_a_file(
    fake_manim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_BEHAVIOUR", "nothing")
    with pytest.raises(render.RenderError) as excinfo:
        render_video({"text": "Ghost"}, fake_manim / "out", "ghost")
    assert excinfo.value.code == "render_output_missing"


# ------------------------------------------------------------------- timeout


def test_timeout_kills_the_process(fake_manim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    heartbeat = fake_manim / "heartbeat"
    monkeypatch.setenv("FAKE_BEHAVIOUR", "hang")
    monkeypatch.setenv("FAKE_HEARTBEAT", str(heartbeat))
    monkeypatch.setitem(render.QUALITY_PRESETS, "preview", _preset(timeout=1.5))

    with pytest.raises(render.RenderError) as excinfo:
        render_video({"text": "Never finishes"}, fake_manim / "out", "hangs")

    assert excinfo.value.code == "render_timeout"
    assert "1.5" in excinfo.value.message
    assert excinfo.value.params == {"limit": 1.5}
    assert heartbeat.is_file(), "the renderer had started before it was killed"

    last_beat = heartbeat.stat().st_mtime_ns
    time.sleep(1.2)
    assert heartbeat.stat().st_mtime_ns == last_beat, "the timed-out renderer is still running"
    assert _leftover_work_dirs() == set()


# ------------------------------------------------------- no shell anywhere


def test_no_shell_true_anywhere_in_the_python_source() -> None:
    """AGENTS.md section 5: the pattern is split so this file cannot match itself."""
    pattern = re.compile(r"shell\s*=\s*True")
    sources = [*_ROOT.glob("backend/**/*.py"), *_ROOT.glob("scripts/**/*.py")]
    offenders = [
        path.relative_to(_ROOT).as_posix()
        for path in sources
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ------------------------------------------------------------- one real clip


def test_renders_a_one_second_real_preview_clip(tmp_path: Path) -> None:
    video = render_video(
        {
            "text": "Real Manim",
            "animation": "write",
            "animation_duration": 1,
            "hold_duration": 0.5,
            "text_color": "#FFFFFF",
            "background": {"type": "solid", "color": "#000000"},
            "quality": "preview",
        },
        tmp_path,
        "real-preview",
    )
    assert video.is_file()
    assert video.stat().st_size > 1000
    assert _leftover_work_dirs() == set(), "the working directory must be removed"

    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == (854, 480)
        assert float(stream.average_rate) == pytest.approx(15.0)
        frames = [frame.to_ndarray(format="rgb24") for frame in container.decode(stream)]

    assert frames[0].shape[:2] == (480, 854)
    # 1 s of animation plus a 0.5 s hold at 15 fps.
    assert 15 <= len(frames) <= 30
    assert _lit_pixels(frames[-1]) > 500, "the text must be fully drawn by the end of the clip"
    assert _lit_pixels(frames[len(frames) // 3]) < _lit_pixels(frames[-1]), (
        "Write must reveal the text over time"
    )


def test_real_render_writes_a_file_the_api_can_serve(tmp_path: Path) -> None:
    """The moved file keeps the requested name, which later becomes the cache key."""
    video = render_video(
        {"text": "Named", "animation_duration": 0.5, "hold_duration": 0}, tmp_path, "abc123-preview"
    )
    assert video.parent == tmp_path
    assert video.name == "abc123-preview.mp4"
    assert not list(tmp_path.glob("*.log"))
