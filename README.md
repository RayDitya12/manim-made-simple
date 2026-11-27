# Manim Made Simple

Create animated text videos in Manim style, no coding needed.

An open-source desktop app (Electron + FastAPI + Manim Community Edition) for
non-technical users: fill in a form, press Generate, watch the video, tweak,
download. Runs fully offline on your machine.

This project is independent. It is not affiliated with or endorsed by the
Manim Community, Grant Sanderson, or 3Blue1Brown.

## Status

Early development. See `SPEC.md` for the product specification and
`ROADMAP.md` for the build order.

## Development setup

Requires Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS
pip install -r requirements-dev.txt
```

Run the checks:

```bash
ruff check .
ruff format --check .
pytest -q
```

Run the backend and ask it what this machine can do:

```bash
python -m backend.app.main            # serves http://127.0.0.1:8000
curl http://127.0.0.1:8000/api/health  # environment self-check
curl http://127.0.0.1:8000/api/options # allowed values and limits for the form
```

The same server also serves the interface: open `http://127.0.0.1:8000/` in a browser,
fill in the form, and press **Generate preview**. The form is built from `/api/options`, so
its choices and limits are exactly the ones this backend enforces.

Render a video and poll the job:

```bash
curl -X POST http://127.0.0.1:8000/api/render \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello World","animation":"typewriter"}'
# {"job_id":"76fd1a76...","cached":false}

curl http://127.0.0.1:8000/api/jobs/76fd1a76...
# {"job_id":"...","status":"done","video_url":"/api/videos/<sha256>-preview.mp4","error":null}
```

Fetch the video the job reported:

```bash
curl -O http://127.0.0.1:8000/api/videos/<sha256>-preview.mp4   # streams, and answers Range
curl -I "http://127.0.0.1:8000/api/videos/<sha256>-preview.mp4?download=1"
# content-disposition: attachment; filename="<sha256>-preview.mp4"
```

Only names this app produced are servable: the name must be a 64-character cache key,
one of the quality ids, and `.mp4`. Anything else (including `../` in any spelling) is
rejected with `400 {"error":{"code":"video_name_invalid", ...}}` before the filesystem is
touched.

The server also answers only a loopback `Host` header, and it names an origin in CORS only
for `http://127.0.0.1:*` or `http://localhost:*`. A webpage the user happens to visit cannot
drive the backend through the visitor's own machine: the request is refused with
`400 {"error":{"code":"host_not_allowed", ...}}` before any route runs.

Rendered videos are cached in the per-user data directory and reused when the
parameters match. `MTS_DATA_DIR` says where; without it the OS default is used
(`%LOCALAPPDATA%\manim-made-simple` on Windows,
`~/.local/share/manim-made-simple` on Linux,
`~/Library/Application Support/manim-made-simple` on macOS). Electron sets it to
its `userData` folder in Phase 6.

The packaged app bundles its own Python (via python-build-standalone), Manim
and FFmpeg; those fetch scripts arrive in later roadmap phases. During
development the backend runs against your activated Python environment.

## Repository layout

- `backend/` — FastAPI app, the fixed Manim scene template, bundled fonts, tests
- `ui/` — plain HTML/CSS/JS frontend with English and Indonesian locales
- `desktop/` — Electron shell (added in Phase 6)
- `scripts/` — runtime fetch and build helpers
- `.github/workflows/` — CI (added in Phase 5)
