# SPEC.md

Name: **Manim Made Simple** (repository and package slug: `manim-made-simple`). The short form "MMS" is for developer docs only and must not appear in user-facing text. Keep the name in one place, `app_meta.json`.

This project is independent. It is not affiliated with or endorsed by the Manim Community, Grant Sanderson, or 3Blue1Brown. The README and the app's About screen must say so.

This file is the source of truth for behavior. If code and this file disagree, this file wins. If this file is unclear or contradicts itself, stop and ask the owner.

## 1. Goal

A free, open-source desktop app that lets a non-technical person create clean animated-text videos in the style of Manim (the library popularized by 3Blue1Brown). The user fills in a form, presses a button, watches the rendered video in the app, tweaks settings, renders again, and downloads when satisfied.

There is **no real-time preview**. The loop is: edit settings, press Generate, wait a few seconds, watch the video, repeat.

### Non-goals (v1)

- No real-time or in-browser preview engine.
- No hosted/public render service. Everything runs on the user's machine.
- No user accounts, no telemetry, no network calls after install.
- No LaTeX or math typesetting (`MathTex`, `Tex`). Plain `Text` only. This keeps the install small.
- No arbitrary user code, plugins, or scripting.
- No timeline editor, multiple text layers, or audio.

## 2. Target users

YouTubers, educators, and video editors who want animated text (intros, titles, lower thirds) without learning After Effects or Python. Assume zero technical skill. Never show stack traces to the user.

## 3. Platforms

| Platform | Status | Package |
|---|---|---|
| Windows 10/11 x64 | **Official**, tested by the owner | NSIS installer (`.exe`) |
| Linux x64 | **Official**, tested in CI and a VM | AppImage |
| macOS (arm64 + x64) | **Experimental**, built in CI, untested, unsigned | `.dmg` |

macOS notes: no code signing in v1. Users must open it via System Settings > Privacy & Security > "Open Anyway". The README must say macOS is experimental. Never block a release because the macOS build fails; mark it as allowed-to-fail in CI.

## 4. Architecture

```
Electron (UI shell)
  └─ spawns ──> FastAPI backend (127.0.0.1, random free port)
                  └─ runs ──> bundled Python + Manim CE ──> bundled FFmpeg
```

Development happens in stages (see ROADMAP.md):

1. **Local web app**: FastAPI + plain HTML/JS, run from a terminal. Proves the render loop.
2. **Electron wrapper**: same UI and backend, bundled runtimes, installer.
3. **CI**: GitHub Actions builds Windows, Linux, and macOS (experimental) installers.

### Stack

- Python 3.11, **Manim Community Edition** (`from manim import *`, never ManimGL / `manimlib`).
- FastAPI + Uvicorn, Pydantic v2 for validation.
- Frontend: plain HTML, CSS, and ES-module JavaScript. **No framework, no build step.**
- Electron + electron-builder for packaging.
- FFmpeg binary bundled (never taken from system PATH in packaged builds).
- Python runtime bundled via python-build-standalone, with Manim pre-installed into it.
- Tests: pytest (backend), Playwright (UI smoke test, optional).
- Lint/format: ruff (Python), prettier (JS/CSS/HTML).

### Repository layout

```
/
├─ AGENTS.md  SPEC.md  ROADMAP.md  README.md  LICENSE (once owner decides)
├─ backend/
│  ├─ app/            # FastAPI app: main.py, models.py, render.py, jobs.py, cache.py
│  ├─ scenes/         # text_scene.py (the ONE fixed Manim template)
│  ├─ fonts/          # OFL fonts + LICENSES/ folder with each license text
│  └─ tests/
├─ ui/                # index.html, styles.css, app.js, i18n.js, locales/ (en.json, id.json)
├─ desktop/           # Electron main.js, preload.js, electron-builder config
├─ scripts/           # fetch-runtimes (python, ffmpeg), build helpers
└─ .github/workflows/ # ci.yml, release.yml
```

## 5. Render parameters

The UI form and the API use exactly this schema. The backend exposes the allowed values through `GET /api/options` and the UI **builds its controls from that response**, so limits live in one place.

| Field | Type | Rules | Default |
|---|---|---|---|
| `text` | string | 1 to 100 chars, max 3 lines, no control characters except `\n` | `"Hello World"` |
| `animation` | enum | `write`, `fade_in`, `typewriter` | `write` |
| `animation_duration` | number (s) | 0.5 to 10 | 2 |
| `hold_duration` | number (s) | 0 to 10 | 1 |
| `font` | enum | id from the font whitelist | first in list |
| `font_size` | integer | 24 to 200 | 96 |
| `text_color` | string | `^#[0-9A-Fa-f]{6}$` | `#FFFFFF` |
| `outline_color` | string | same regex | `#000000` |
| `outline_width` | number | 0 to 10 (0 = no outline) | 0 |
| `background` | object | `{"type":"solid","color":"#RRGGBB"}` or `{"type":"transparent"}` (transparent from Phase 4) | solid `#000000` |
| `quality` | enum | `preview` (854x480, 15 fps) or `hd` (1920x1080, 60 fps) | `preview` |

Total video length (animation + hold) must not exceed 20 seconds. Reject anything outside these rules with HTTP 422 and an error `code` (see section 6).

Rules for the template:

- Use `Text` with the chosen font. Outline is drawn behind the fill (stroke with `background=True`), so the text stays readable.
- `write` maps to Manim `Write`, `fade_in` to `FadeIn`, `typewriter` to `AddTextLetterByLetter` (or `TypeWithCursor` if the installed Manim version has it and it looks right).
- Verify every Manim class, parameter, and CLI flag against the installed version (`manim render --help`, `help(...)`, docs). Do not rely on memory.

## 6. API contract

All endpoints are under `/api`, bound to `127.0.0.1` only.

| Method + path | Purpose | Response |
|---|---|---|
| `GET /api/health` | Self-check | `{status, python_version, manim_version, ffmpeg_version, ffmpeg_source, error}` |
| `GET /api/options` | Allowed values and limits | fonts, animations, limits, defaults |
| `POST /api/render` | Start a render job | `202 {job_id, cached}` |
| `GET /api/jobs/{job_id}` | Poll status | `{job_id, status: queued\|running\|done\|failed, video_url, error}`; unused keys are `null` |
| `GET /api/videos/{name}` | Serve a rendered file | video bytes; `?download=1` sets `Content-Disposition: attachment` |

Behavior:

- `GET /api/health` names the interpreter, Manim, and FFmpeg versions actually in use. `ffmpeg_source` is `cli` or `pyav`: Manim CE encodes through PyAV, so a missing `ffmpeg` binary is a warning, not a failure. When the machine cannot render, `status` is `degraded`, the missing version is `null`, and `error` holds a stable `code` (`manim_missing`, `ffmpeg_missing`) with an English `message` the UI can fall back to.
- Jobs run on a single in-process worker (one render at a time, FIFO). No Redis, no Celery.
- A request that passes validation always answers with a `job_id`, so the UI has one polling path. When the video is already in the cache, the job is created in state `done` and nothing is queued. An empty body `{}` is a valid request: every field in section 5 has a default.
- A request that duplicates a render already queued or running creates a second job. The worker re-checks the cache immediately before it starts Manim, so the second job finishes without a second render.
- Stopping the queue (app quit) does not interrupt a render in progress; killing the child process is the Electron shell's job (section 10, task 6.1). Jobs not yet started stay `queued`.
- **Cache key** = SHA-256 of canonical JSON (sorted keys) of validated params + app version + Manim version + template version. Same key means serve the existing file and return `cached: true`. When the Manim version cannot be determined, the request is refused with `503 {code: manim_missing}` rather than keyed on a guess.
- Video file names are `<sha256>-<quality>.<ext>`. The video endpoint must validate `name` against a strict regex and resolve inside the output directory only (no path traversal). `GET /api/jobs/{job_id}` validates `job_id` against the generator's own shape (`^[0-9a-f]{32}$`) before any lookup, and an unknown or malformed id is `404 {code: job_not_found}`.
- Output directory: a per-user data directory (`MTS_DATA_DIR` env var; Electron sets it to `app.getPath('userData')`). The terminal workflow leaves it unset, which resolves to `%LOCALAPPDATA%\<app>` on Windows, `~/.local/share/<app>` on Linux (`$XDG_DATA_HOME` when set), `~/Library/Application Support/<app>` on macOS; rendered videos live in a `videos/` folder inside it. Add a "Clear cache" action; also delete files older than 7 days on startup.
- Render timeout: 120 s for `hd`, 60 s for `preview`. On timeout, kill the process tree and return a friendly error.
- Errors returned by the API have a stable `code` (for example `text_too_long`, `render_timeout`, `params_not_json` for a body that is not JSON, `job_not_found`), a short English `message`, and optional `params`. The UI shows the translation of the `code` from the locale files and falls back to `message`. Full technical details go to a log file, never to the UI.
- `GET /api/jobs/{job_id}` always returns the same four keys (`job_id`, `status`, `video_url`, `error`), with `null` where a value does not apply yet, so the UI never has to distinguish "absent" from "not ready".

## 7. Rendering

- Run Manim through `subprocess` with a **list of arguments**, never `shell=True`.
- Pass parameters to the template as a **JSON file** whose path is given in an env var (`MTS_PARAMS`). The template reads and re-validates it. Parameters are never interpolated into source code or command strings.
- Resolution and fps come from the quality preset (`-r W,H --fps N` or equivalent; verify flags).
- Transparent background (Phase 4): WebM (VP9 with alpha) and/or MOV (ProRes 4444) through Manim's transparent mode. Browser playback of transparent video is inconsistent (notably Safari), so the UI shows it over a checkerboard and offers the file for download.
- Gradient background (Phase 4, optional): render transparent, then composite over a generated gradient with FFmpeg.
- On Windows, launch child processes without a console window.

## 8. UI requirements

- One screen. Left: settings form. Right: video player and actions.
- Controls: text box, animation dropdown, duration sliders with numeric input, font dropdown, three color pickers (text, outline, background) plus an outline width slider, background mode (solid / transparent), a **Generate preview** button, and a **Download HD** button.
- Show a clear loading state and a cancel button while rendering. Disable Generate while a job is running.
- After a render finishes, play the video inline with standard controls and keep all settings so the user can tweak and render again.
- Show a progress message, not a fake percentage (render progress is not reliably known).
- Show friendly errors with a "Try again" action.
- Remember the last settings in `localStorage` (inside Electron this is fine).
- Accessibility: keyboard navigable, visible focus, labels on every control, color contrast checked, respects `prefers-reduced-motion` and `prefers-color-scheme`.
- **Languages: English (`en`) and Indonesian (`id`).** The UI language follows the system language by default. Inside Electron, read `app.getLocale()` in the main process and pass it to the UI through the preload script. In a plain browser, use `navigator.languages`. Any locale starting with `id` selects Indonesian; everything else falls back to English.
- A small language switcher lets the user override the automatic choice. The override is remembered in `localStorage`.
- All visible text (labels, buttons, tooltips, errors, About screen, page title) comes from `ui/locales/en.json` and `ui/locales/id.json` through `ui/i18n.js`. No hard-coded visible strings in HTML or JS. Both files must always contain the same keys, enforced by a test that runs in CI.
- Use `Intl` APIs for numbers and units. Layouts must tolerate longer Indonesian strings (no fixed-width buttons that clip text).
- Text the user types for the video is never translated. Indonesian uses Latin script, so every bundled font works for both languages.
- The agent writes the Indonesian copy; the owner reviews it in the localization task (ROADMAP 3.5).
- Design: should look intentionally designed, not like a default AI-generated dashboard. See the skills section in AGENTS.md.

## 9. Fonts and licensing

- Only fonts under the SIL Open Font License (OFL) or similarly permissive licenses may be bundled. Keep each font's license text in `backend/fonts/LICENSES/`.
- Initial whitelist (verify license of each file before adding): Inter, Poppins, Montserrat, Roboto, Playfair Display, Bebas Neue, Fira Code.
- Font registration with Manim differs by OS. **Verify on Windows and Linux** that a bundled font really renders. Do not assume `register_font` works everywhere. If it does not, use an OS-appropriate fallback and document it.
- App license: **undecided** (see open decisions). Do not add a LICENSE file until the owner decides.
- Dependencies: record the license of every new dependency in the PR description.

## 10. Security

The app runs locally, but it still treats all input as untrusted.

- No code from the user is ever executed. Only the validated fields in section 5 reach the template.
- Backend binds `127.0.0.1` only. Enable CORS only for the Electron origin / localhost UI origin. Reject requests with an unexpected `Host` header.
- Electron: `contextIsolation: true`, `nodeIntegration: false`, a restrictive Content-Security-Policy, no remote content loaded, `webSecurity` left on, external links opened in the system browser.
- No network access at runtime. No auto-update in v1.
- No secrets in the repository. No bundled binaries committed to git (they are fetched by `scripts/` and in CI).

## 11. Quality bar

- Backend: pytest covers validation (good and bad input), cache key stability, path traversal rejection, and one real end-to-end render of a short clip.
- A short preview render of simple text should finish in roughly 5 to 20 seconds on an ordinary laptop. If it is much slower, report it before adding features.
- Every PR passes CI (lint, tests). UI changes include a screenshot in the PR.
- Installer smoke test (Windows, Phase 6): install, launch, render a sample, play it, uninstall.

## 12. Known risks (check early, do not guess)

1. **Manim system libraries.** Manim depends on Cairo and Pango. Wheels exist for some pieces, but verify that the bundled Python works on a clean Windows machine and a clean Linux install. On Linux, system libraries may need to be bundled or documented.
2. **Installer size.** Bundled Python + Manim + FFmpeg + Chromium will be several hundred MB. That is accepted.
3. **Per-OS builds.** Installers cannot be cross-compiled. Build each OS in its own CI runner.
4. **macOS.** Unsigned, untested, experimental (section 3).
5. **Font registration** differs per OS (section 9).
6. **Antivirus false positives** on unsigned Windows installers are possible. Document in the README.

## 13. Open decisions (owner decides, agent must ask)

1. App license (MIT, Apache-2.0, or AGPL-3.0). AGPL would stop others from turning the project into a closed online service.
2. Which GitHub account or organization owns the repository.
3. Whether to add a donation link (GitHub Sponsors / Ko-fi / Saweria) to the README.

Already decided: the name is Manim Made Simple, and the UI is English + Indonesian following the system language.
