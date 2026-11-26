# ROADMAP.md

Build in this order. Each task is one pull request unless noted. Tick the checkbox in the same PR that completes the task. Follow AGENTS.md for the Git workflow.

"Acceptance" lists what must be demonstrated with real command output.

## Phase 0: Repository

- [x] **0.1 Bootstrap repo `manim-made-simple` and protect `main`** (AGENTS.md section 3.1)
  Acceptance: repo exists on GitHub; direct push to `main` is rejected (show the error); squash-only merges enabled.
- [ ] **0.2 Project scaffolding**: folder layout from SPEC.md section 4, `.gitignore`, `pyproject.toml` (ruff, pytest), `requirements.txt`, README with dev setup.
  Acceptance: `ruff check .` and `pytest -q` run (even with one trivial test).

## Phase 1: Render engine (no web yet)

- [ ] **1.1 Environment check**: `scripts/check_env.py` verifies Python version, Manim CE, FFmpeg, and prints their versions.
  Acceptance: script prints versions on the dev machine and exits non-zero with a clear message when something is missing.
- [ ] **1.2 Fixed Manim template** `backend/scenes/text_scene.py`: reads params from the JSON file at `MTS_PARAMS`, supports all three animations, text color, outline, solid background.
  Acceptance: hand-written params JSON produces a 480p MP4 for each animation; outline visibly sits behind the fill.
- [ ] **1.3 Pydantic models and validation** (`models.py`) exactly per SPEC.md section 5.
  Acceptance: tests cover valid input, every boundary, bad hex colors, too-long text, unknown animation/font.
- [ ] **1.4 Render function** (`render.py`): validate, write params JSON, run Manim via `subprocess` (arg list, timeout), return the output path.
  Acceptance: test renders a 1-second preview clip; timeout test kills the process; no `shell=True` anywhere (grep shows none).
- [ ] **1.5 Fonts**: add whitelisted OFL fonts with license texts; verify each really renders.
  Acceptance: a test renders one frame per font and shows the font actually differs. Note any OS-specific registration findings in the PR.

## Phase 2: Backend API

- [ ] **2.1 FastAPI app** with `GET /api/health` and `GET /api/options`.
  Acceptance: `curl` outputs shown; options match SPEC limits.
- [ ] **2.2 Cache** (`cache.py`): canonical-JSON SHA-256 key, file naming, cleanup of files older than 7 days.
  Acceptance: tests prove same params give the same key, any changed field gives a different key.
- [ ] **2.3 Job queue** (`jobs.py`): single worker, `POST /api/render`, `GET /api/jobs/{id}`.
  Acceptance: render a sample via `curl`, poll to `done`, second identical request returns `cached: true` instantly; invalid payload returns 422 with a stable error `code` and a short English message.
- [ ] **2.4 Video endpoint** with strict name validation and `?download=1`.
  Acceptance: path traversal attempts (`../`, encoded variants) are rejected in tests; valid file streams and plays.
- [ ] **2.5 Host and CORS hardening**: bind `127.0.0.1`, reject unexpected `Host` headers.
  Acceptance: tests show a bad `Host` is rejected.

## Phase 3: User interface

- [ ] **3.1 UI skeleton** served by FastAPI: form built from `/api/options`, Generate button, loading state, video player. (Use the `modern-web-guidance` skill first.) Set up i18n from the start: `ui/i18n.js` plus `locales/en.json` and `locales/id.json`, no hard-coded visible strings.
  Acceptance: from a browser, change text and color, press Generate, see the video play. Screenshot attached.
- [ ] **3.2 Full controls**: duration sliders, font dropdown, three color pickers, outline width, Download HD, cancel, friendly errors, settings remembered in `localStorage`.
  Acceptance: manual walkthrough of every control with screenshots; error state shown by stopping the backend.
- [ ] **3.3 Accessibility pass**: keyboard use, focus, labels, contrast, reduced motion.
  Acceptance: checklist in the PR; keyboard-only run-through described.
- [ ] **3.4 Design pass** with the `heydamir/unslop` skill (UI only, SPEC.md section 8 still fully satisfied). Owner approves the direction first.
  Acceptance: before/after screenshots; all controls still work.
- [ ] **3.5 Localization complete**: automatic language from the system locale (Electron `app.getLocale()` via preload, browser `navigator.languages`), language switcher with remembered override, error codes translated, parity test for both locale files.
  Acceptance: screenshots of the full UI in English and Indonesian; a test fails when a key exists in only one file; forcing a system locale of `id-ID` and `en-US` selects the right language; no layout clipping in Indonesian. The owner reviews the Indonesian copy.

## Phase 4: Extra features

- [ ] **4.1 Transparent background** (WebM and/or MOV), checkerboard preview, download.
  Acceptance: output file has an alpha channel (show the `ffprobe` pixel format); verified over a colored background in a video editor or browser.
- [ ] **4.2 Gradient background** (optional): composite with FFmpeg.
  Acceptance: render with a two-color gradient; screenshot attached.
- [ ] **4.3 Presets**: a few ready-made style presets in the UI (intro title, lower third, subtitle) as plain JSON in the repo.
  Acceptance: selecting a preset fills the form; rendering each produces a valid video.

## Phase 5: CI

- [ ] **5.1 CI workflow** `.github/workflows/ci.yml`: ruff, pytest (including one real short render), on Linux and Windows. *(Owner review required.)*
  Acceptance: green run link in the PR.
- [ ] **5.2 Require the CI check on `main`** using the real job name (AGENTS.md section 3.1). *(Owner review required.)*
  Acceptance: a PR with failing CI cannot be merged (show it).

## Phase 6: Desktop app, Windows first

- [ ] **6.1 Electron shell**: spawn the backend on a random free port (127.0.0.1), wait for `/api/health`, open the window, kill the child on quit and on crash.
  Acceptance: `npm start` runs the full app against the development Python; closing the window leaves no stray process (show the process list).
- [ ] **6.2 Electron security settings** per SPEC.md section 10. *(Owner review required.)*
  Acceptance: test or screenshot shows `contextIsolation` on, `nodeIntegration` off, CSP active, external links open in the system browser.
- [ ] **6.3 Runtime fetch scripts**: `scripts/` downloads python-build-standalone and FFmpeg for the target OS, installs Manim into that Python.
  Acceptance: on a clean folder, the script produces `resources/python` and `resources/ffmpeg`; `resources/python -m manim --version` works; nothing is committed to git.
- [ ] **6.4 Packaged Windows installer** with electron-builder (NSIS). Backend uses only the bundled Python and FFmpeg via env vars, never PATH. Startup self-check screen reports a missing component clearly.
  Acceptance: installed on a machine or VM **without** Python or FFmpeg, launch, render a sample, play, uninstall cleanly. Record the installer size.
- [ ] **6.5 Data directory**: `MTS_DATA_DIR` set to Electron's `userData`; "Clear cache" action works.
  Acceptance: videos appear in `userData`, never next to the executable.

## Phase 7: Linux

- [ ] **7.1 AppImage build** on a Linux runner/VM.
  Acceptance: runs on a clean Linux install; Cairo/Pango handling documented (bundled or required packages listed in README).

## Phase 8: macOS (experimental)

- [ ] **8.1 macOS builds in CI** (arm64 and x64), marked allowed-to-fail, labeled experimental in the README with the "Open Anyway" steps. *(Owner review required.)*
  Acceptance: CI produces `.dmg` artifacts; README caveat present. No claim that it works unless someone has tested it.

## Phase 9: Release

- [ ] **9.1 Release workflow**: on a version tag, CI builds all platforms and attaches installers to a GitHub Release. *(Owner review required.)*
  Acceptance: a test tag (`v0.1.0-rc1`) produces a release with Windows and Linux installers.
- [ ] **9.2 README and docs**: install steps per OS, screenshots, known issues (antivirus warning, unsigned macOS), a clear non-affiliation notice (not associated with Manim Community or 3Blue1Brown), contribution guide, license (after the owner decides). README in English, plus `README.id.md` in Indonesian.
- [ ] **9.3 Decide open items** with the owner (SPEC.md section 13) and finalize the LICENSE.

## Later ideas (not scheduled)

- More animations (Transform between two texts, letter-by-letter color change)
- Export presets for common sizes (square, vertical)
- Apple code signing if funding appears
