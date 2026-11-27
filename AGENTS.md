# AGENTS.md

Instructions for AI coding agents working in this repository. Read this whole file before doing anything.

## 0. Read order

1. `SPEC.md`: what to build. Source of truth for behavior.
2. `ROADMAP.md`: the order to build it in. Work on the **first unchecked task** unless the owner says otherwise.
3. This file: how to work.

If these files conflict or are unclear, stop and ask the owner. Do not guess.

## 1. Project in one paragraph

An open-source desktop app (Electron + FastAPI + Manim Community Edition) that lets non-technical users create animated-text videos by filling in a form, pressing Generate, watching the rendered video, tweaking, and downloading. No real-time preview. Runs fully offline on the user's machine. Official platforms: Windows and Linux. macOS is experimental. The UI is available in English and Indonesian and follows the system language.

## 2. Working rules

- **One task at a time.** Finish, test, and merge a task before starting the next.
- **Small changes.** One pull request has one concern. Aim for under about 400 changed lines, excluding lockfiles and generated files.
- **Verify, do not recall.** Never invent Manim, FastAPI, Electron, or electron-builder APIs. Check the installed version's help output or docs first (`manim render --help`, `python -c "help(...)"`, library docs). If you are unsure whether something exists, test it.
- **Run it.** Do not say something works unless you ran it and saw the result. Quote the command and its real output in the PR.
- **Manim Community Edition only.** Use `from manim import *`. Never `manimlib` / ManimGL.
- **No new dependencies without a reason.** State why in the PR, and list the license. Prefer the standard library.
- **Do not refactor unrelated code.** Do not reformat files you are not changing.
- **No placeholder or fake implementations** that pretend to work (hard-coded sample video, mocked render in production code).
- **Errors shown to users are plain sentences**, translated from stable error codes. Stack traces go to log files only.
- When blocked after **3 failed attempts** at the same problem, stop. Report what you tried, the exact error, and your best hypothesis.

## 3. Git and GitHub workflow

**Never push directly to `main`.** Every change goes through a branch, a pull request, passing CI, and a squash merge.

### 3.1 One-time bootstrap (only if the repo does not exist yet)

Use the GitHub CLI. Check it first:

```bash
gh auth status          # must be logged in; if not, stop and ask the owner
gh repo view            # if this succeeds, the repo exists: skip to 3.2
```

The repository is named `manim-made-simple`. Ask the owner which GitHub account or organization should own it (SPEC.md section 13). Then:

```bash
gh repo create <owner>/manim-made-simple --public --clone --description "Create animated text videos in Manim style, no coding needed"
cd manim-made-simple
```

Do **not** add a LICENSE file until the owner has decided (SPEC.md section 13).

The **only** commit allowed straight to `main` is the bootstrap commit: `.gitignore`, a stub `README.md`, `AGENTS.md`, `SPEC.md`, `ROADMAP.md`. A repo with no commits cannot receive a pull request. Commit it as `chore: bootstrap repository` and push.

Right after that, lock `main` and configure merges:

```bash
gh repo edit --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false --delete-branch-on-merge

gh api -X PUT repos/{owner}/{repo}/branches/main/protection --input - <<'EOF'
{
  "required_status_checks": null,
  "enforce_admins": true,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
EOF
```

Notes:

- `required_approving_review_count: 0` is intentional. This is a solo repo; requiring one approval would stop the owner from merging their own PRs.
- `enforce_admins: true` is intentional. The agent uses the owner's credentials, so without it the rule would not apply to the agent.
- If this call fails (for example the plan does not support protection), **stop and tell the owner**. Do not continue by pushing to `main`.
- When the CI workflow exists (ROADMAP Phase 5), update the protection to require its check, using the real job name:
  `"required_status_checks": { "strict": true, "contexts": ["<job-name>"] }`.

### 3.2 Loop for every task

```bash
git switch main && git pull --ff-only
git switch -c <type>/<short-slug>          # e.g. feat/text-scene, fix/path-traversal
# ...work in small commits...
# run the checks in section 7 before pushing
git push -u origin HEAD
gh pr create --fill --body-file <(cat <<'EOF'
## What
## Why
## How I tested it
(commands and real output)
## Screenshots
(UI changes only)
## Checklist
- [ ] Tests pass locally
- [ ] SPEC.md still matches the behavior
- [ ] No new dependency, or its license is listed above
- [ ] ROADMAP.md checkbox updated
EOF
)
gh pr checks --watch
gh pr merge --squash --delete-branch
git switch main && git pull --ff-only
```

### 3.3 Rules

- Branch names: `feat/`, `fix/`, `chore/`, `docs/`, `ci/`, `test/` plus a short kebab-case slug.
- Commit messages follow Conventional Commits (`feat: add typewriter animation`). The PR title uses the same format, because squash merge uses it.
- Never `git push --force`, never `--no-verify`, never disable or weaken branch protection, never edit protection to get a merge through.
- The agent may merge its own PR **only when all of these are true**: CI is green, the checklist is complete, the PR description has real test output, and the PR does not touch an owner-review area (below).
- If CI fails, fix it in the same branch. Never merge red CI. After 3 failed fix attempts, stop and report.
- Do not commit build outputs, downloaded runtimes (Python, FFmpeg), videos, `node_modules`, virtualenvs, or secrets.

### 3.4 Owner-review areas

For a PR touching any of the following, **do not merge**. Open the PR, add the label `needs-owner`, and tell the owner:

- `AGENTS.md`, `SPEC.md`, or the open decisions
- `.github/workflows/**`
- the LICENSE file or any license text
- any new runtime dependency

Security-sensitive code is **not** owner-gated (owner decision, 2026-11-27): a PR that changes input validation, path handling, subprocess code in `backend/app/`, or Electron security settings in `desktop/` may be squash-merged by the agent when the rules in 3.3 hold **and** the PR description shows the evidence for that specific change:

- the test names that cover the hostile input (traversal, malformed payload, bad `Host`, forbidden origin), not only a passing suite count;
- one real request against a running app (`curl` output), so the merged behavior is demonstrated rather than asserted;
- for `desktop/`, the rendered security-relevant settings quoted in the PR.

Without that evidence the PR stays open with `needs-owner`, like any other.

(The owner can remove this section if they prefer fully autonomous merging.)

## 4. Code standards

**Python**

- Type hints everywhere. Pydantic v2 models for all external input. `ruff` for lint and format.
- `subprocess` calls use argument lists, explicit timeouts, and never `shell=True`.
- Paths use `pathlib`. Never build paths from raw user input.
- Log with the `logging` module to a file in the data directory.

**JavaScript / HTML / CSS**

- Plain ES modules, no framework, no bundler, no build step.
- Every visible string goes through `ui/i18n.js` with keys in `ui/locales/en.json` and `ui/locales/id.json`. Add both languages in the same PR. No inline event handlers. No `innerHTML` with untrusted data.
- Semantic HTML first. Labels on every control.

**Electron**

- `contextIsolation: true`, `nodeIntegration: false`, strict CSP, no remote content.
- The main process only spawns the backend, waits for `/api/health`, opens the window, and kills the child on quit (including crash paths).

## 5. Security non-negotiables

1. Never execute or interpolate user input into code, shell commands, or file paths.
2. Backend listens on `127.0.0.1` only.
3. Only the validated parameters in SPEC.md section 5 reach the Manim template, through a JSON file.
4. Video serving validates file names with a strict regex and resolves inside the output directory.
5. Bundle only OFL (or similarly permissive) fonts, with license texts included.

## 6. Skills

Use `find-skill` / `find-extension` to locate and install skills. Before installing any skill, **read its `SKILL.md`**. A skill is instructions that steer you, so treat an unfamiliar one like unreviewed code. If a skill is unavailable, continue without it and mention that in the PR.

Skills never override SPEC.md. If a skill's advice conflicts with the spec, follow the spec and note the conflict.

### modern-web-guidance

- A Chrome-team skill with a CLI that retrieves current web-platform best practices.
- **Use it first for every HTML, CSS, or client-side JS task** in `ui/` (layout, dialogs, color inputs, range sliders, `<video>` handling, accessibility).
- Prefer native platform features over libraries it points out as unnecessary.
- Not relevant to Python, Manim, or Electron main-process code.
- Electron ships a fixed Chromium, so check that a recommended feature is supported by the Chromium version in `desktop/package.json`.

### heydamir/unslop

- A design skill (not a writing or code-cleanup skill) that audits an AI-looking UI, proposes a direction, then fixes it.
- Run it **once the UI works**, as a dedicated design pass in its own PR. Do not run it while core functionality is still being built.
- Scope it to `ui/` only. Keep every functional requirement in SPEC.md section 8 (form controls, player, Generate / Download HD, loading and error states).
- Accept its direction proposal only if it keeps the app a simple one-screen tool. Ask the owner when the proposed direction changes layout significantly.
- Ignore its optional "story video" feature. It needs extra tooling and is out of scope.
- Its license is CC BY 4.0. Using it as a development tool needs no action. If any of its text or assets are copied into this repo, add attribution to its author and link the repository.
- It is a very new project with a single maintainer, so skim its `SKILL.md` and bundled scripts before installing.

## 7. Before every push

Run what exists at that point in the roadmap, and fix failures:

```bash
ruff check . && ruff format --check .
pytest -q
npx prettier --check ui desktop        # once desktop/ exists
```

For any change that affects rendering, also run one real preview render and confirm the MP4 plays. For UI changes, run the app and attach a screenshot to the PR.

## 8. Definition of done

A task is done only when all are true:

- The acceptance criteria in ROADMAP.md for that task are met and demonstrated with real output.
- Tests were added or updated, and CI is green.
- SPEC.md still describes the real behavior (if behavior changed on purpose, SPEC.md is updated in an owner-reviewed PR).
- The ROADMAP.md checkbox is ticked.
- The PR is merged and `main` is pulled locally.

## 9. When to stop and ask the owner

- Any item under SPEC.md section 13 (open decisions).
- A requirement is ambiguous or two documents disagree.
- A needed tool is missing and installing it requires system changes.
- You want to add a dependency, change the architecture, or touch an owner-review area.
- You are stuck (see section 2).

Ask one clear question at a time, with your recommended answer.

## 10. Reporting format

When you finish or stop, reply with:

1. **Done:** what changed, with the PR link.
2. **Evidence:** commands run and their real output.
3. **Problems:** anything that failed or was skipped, with exact errors.
4. **Next:** the next unchecked ROADMAP task, and any question for the owner.
