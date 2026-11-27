/**
 * The interface: options in, controls built, render requested, video played
 * (ROADMAP task 3.1, SPEC.md section 8).
 *
 * Three rules shape this file:
 *
 * 1. The form is built from `GET /api/options`, never from numbers written here, so a limit
 *    changed in `backend/app/models.py` is the only change needed (SPEC.md section 5).
 * 2. Nothing reaches the DOM as markup. Elements are constructed and filled with
 *    `textContent`, so neither a translation nor the user's own text is ever parsed as HTML.
 * 3. Every string the user reads comes from `i18n.js`. The literals below that look like copy
 *    are the English fallbacks SPEC.md section 6 defines for an error object, and the locale's
 *    `error.*` key wins over each of them whenever it exists.
 */

import {
  DEFAULT_LOCALE,
  applyDocumentTitle,
  applyText,
  errorText,
  loadLocale,
  number,
  t,
} from "/i18n.js";

const ENDPOINTS = {
  options: "/api/options",
  render: "/api/render",
  job: "/api/jobs/",
};

/** How often a running job is polled. Render progress is not reliably known, so the UI shows
 *  a status sentence rather than a percentage (SPEC.md section 8). */
const POLL_MS = 400;

/** The statuses after which polling stops. */
const FINAL_STATUSES = new Set(["done", "failed"]);

/**
 * One entry per SPEC.md section 5 field: the control that edits it and the hint under it.
 * `limit` names the entry in `/api/options` that supplies min and max.
 */
const CONTROLS = {
  text: { kind: "textarea", hint: "hint.text" },
  animation: { kind: "select", hint: null },
  font: { kind: "select", hint: null },
  font_size: {
    kind: "number",
    limit: "font_size",
    step: 1,
    hint: "hint.points",
  },
  animation_duration: {
    kind: "number",
    limit: "animation_duration",
    step: 0.5,
    hint: "hint.seconds",
  },
  hold_duration: {
    kind: "number",
    limit: "hold_duration",
    step: 0.5,
    hint: "hint.seconds",
  },
  text_color: { kind: "color", hint: null },
  outline_color: { kind: "color", hint: null },
  outline_width: {
    kind: "number",
    limit: "outline_width",
    step: 0.5,
    hint: "hint.number",
  },
  background_type: { kind: "select", hint: null },
  background_color: { kind: "color", hint: null },
  quality: { kind: "select", hint: null },
};

/** Field order and grouping on screen; a row with a `legend` becomes a `<fieldset>`. */
const ROWS = [
  { fields: ["text"] },
  { fields: ["animation"] },
  { fields: ["font"] },
  { fields: ["font_size"] },
  {
    fields: ["animation_duration", "hold_duration"],
    legend: "group.duration",
    hint: "hint.total",
  },
  { fields: ["text_color", "outline_color", "outline_width"] },
  { fields: ["background_type", "background_color"] },
  { fields: ["quality"] },
];

/** The ids in the markup this script drives, mapped to the name used below. */
const ELEMENT_IDS = {
  appName: "app-name",
  status: "job-status",
  settings: "settings",
  fields: "fields",
  generate: "generate",
  busy: "busy",
  error: "job-error",
  errorText: "job-error-text",
  player: "player",
  playerEmpty: "player-empty",
};

let options = null;
const view = {};

/** A failure that already has the SPEC.md section 6 shape the error line shows. */
class ApiFailure extends Error {
  constructor(api) {
    super(api.code);
    this.api = api;
  }
}

function fieldId(name) {
  return `f-${name}`;
}

function hintId(name) {
  return `f-${name}-hint`;
}

/** Ask for JSON. Any failure, including a backend that is not listening, becomes an
 *  :class:`ApiFailure` with a code the locale files can translate. */
async function requestJson(url, init) {
  let response;
  try {
    response = await fetch(url, init);
  } catch {
    throw new ApiFailure({
      code: "unreachable",
      message: "the app could not be reached.",
      params: {},
    });
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new ApiFailure(
      body?.error ?? { code: `http_${response.status}`, params: {} },
    );
  }
  return body;
}

/** The value `/api/options` proposes for one field. */
function valueFor(name) {
  if (name === "background_type") {
    return options.defaults.background.type;
  }
  if (name === "background_color") {
    return options.defaults.background.color;
  }
  return options.defaults[name];
}

function valueOf(name) {
  const node = document.getElementById(fieldId(name));
  return node ? node.value : valueFor(name);
}

/** The `<option>` list one select shows, every entry from the options response. */
function choicesFor(name) {
  if (name === "animation") {
    return options.animations.map((id) => ({
      value: id,
      label: t(`option.animation.${id}`),
    }));
  }
  if (name === "font") {
    // Font names are product names, not copy: never translated (SPEC.md section 8).
    return options.fonts.map((id) => ({ value: id, label: id }));
  }
  if (name === "background_type") {
    return options.background_types.map((entry) => ({
      value: entry.id,
      label: t(`option.background.${entry.id}`),
      available: entry.available,
    }));
  }
  return options.qualities.map((quality) => ({
    value: quality.id,
    label:
      `${t(`option.quality.${quality.id}`)} · ` +
      `${quality.resolution} · ${number(quality.fps)} fps`,
  }));
}

function hintNode(key, params, id) {
  const hint = document.createElement("span");
  hint.className = "hint";
  hint.id = id;
  hint.textContent = t(key, params);
  return hint;
}

/** The numbers a hint interpolates, straight from the advertised limits. */
function hintParams(name) {
  if (name === "text") {
    const limits = options.limits.text;
    return { chars: number(limits.max_chars), lines: number(limits.max_lines) };
  }
  const range = options.limits[CONTROLS[name].limit];
  return { min: number(range.min), max: number(range.max) };
}

function buildControl(name) {
  const config = CONTROLS[name];
  let control;
  if (config.kind === "textarea") {
    control = document.createElement("textarea");
    control.rows = options.limits.text.max_lines;
    control.maxLength = options.limits.text.max_chars;
  } else if (config.kind === "select") {
    control = document.createElement("select");
    for (const choice of choicesFor(name)) {
      const entry = document.createElement("option");
      entry.value = choice.value;
      entry.textContent = choice.label;
      entry.disabled = choice.available === false;
      control.append(entry);
    }
  } else {
    control = document.createElement("input");
    if (config.kind === "number") {
      control.type = "number";
      control.min = options.limits[config.limit].min;
      control.max = options.limits[config.limit].max;
      control.step = config.step;
    } else {
      control.type = "color";
    }
  }
  control.id = fieldId(name);
  control.name = name;
  control.value = valueFor(name);
  return control;
}

/**
 * One labelled control: `<label>`, the control, and the persistent helper text that states
 * the limit (modern-web-guidance `forms`: keep constraints visible, not placeholder-only).
 * `groupHintId` adds a row-level hint without replacing the field's own.
 */
function buildField(name, groupHintId) {
  const config = CONTROLS[name];
  const wrap = document.createElement("div");
  wrap.className = "field";

  const label = document.createElement("label");
  label.htmlFor = fieldId(name);
  label.textContent = t(`field.${name}.label`);

  const control = buildControl(name);
  const ownHint = config.hint
    ? hintNode(config.hint, hintParams(name), hintId(name))
    : null;
  const describedBy = [ownHint?.id, groupHintId].filter(Boolean).join(" ");
  if (describedBy) {
    control.setAttribute("aria-describedby", describedBy);
  }

  wrap.append(label, control);
  if (ownHint) {
    wrap.append(ownHint);
  }
  return wrap;
}

function buildRow(row) {
  if (!row.legend) {
    const fields = row.fields.map((name) => buildField(name));
    if (fields.length === 1) {
      return fields[0];
    }
    const wrap = document.createElement("div");
    wrap.className = "row";
    wrap.replaceChildren(...fields);
    return wrap;
  }

  const hint = hintNode(
    row.hint,
    { max: number(options.limits.total_duration_max) },
    "f-total-hint",
  );
  const box = document.createElement("fieldset");
  box.className = "group";
  const legend = document.createElement("legend");
  legend.textContent = t(row.legend);
  box.append(
    legend,
    ...row.fields.map((name) => buildField(name, hint.id)),
    hint,
  );
  return box;
}

function buildForm() {
  view.fields.replaceChildren(...ROWS.map(buildRow));
  for (const name of ["text_color", "outline_color", "background_color"]) {
    document
      .getElementById(fieldId(name))
      .addEventListener("input", paintPreview);
  }
  document
    .getElementById(fieldId("quality"))
    .addEventListener("change", sizePlayer);
  paintPreview();
  sizePlayer();
}

/**
 * Paint the area behind the player with the requested background and text colour, so the form
 * and the video pane agree before anything is rendered. A custom property is the only thing
 * this file writes to a style: the rules stay in `styles.css`.
 */
function paintPreview() {
  view.output.style.setProperty(
    "--preview-background",
    valueOf("background_color"),
  );
  view.output.style.setProperty("--preview-color", valueOf("text_color"));
}

/** Keep the player at the size the chosen quality produces, so the pane does not jump. */
function sizePlayer() {
  const quality = options.qualities.find(
    (entry) => entry.id === valueOf("quality"),
  );
  if (quality) {
    view.player.width = quality.width;
    view.player.height = quality.height;
  }
}

/** The render parameters for the current form, in the SPEC.md section 5 shape. */
function currentParams() {
  const params = {};
  for (const name of Object.keys(CONTROLS)) {
    if (name === "background_type" || name === "background_color") {
      continue;
    }
    const node = document.getElementById(fieldId(name));
    params[name] = node.type === "number" ? Number(node.value) : node.value;
  }
  params.background = {
    type: valueOf("background_type"),
    color: valueOf("background_color"),
  };
  return params;
}

function setStatus(text) {
  view.status.textContent = text;
}

function setBusy(busy) {
  view.generate.disabled = busy;
  view.busy.hidden = !busy;
}

function showError(api) {
  setStatus(t("status.failed"));
  view.errorText.textContent = errorText(api);
  view.error.hidden = false;
}

function hideError() {
  view.error.hidden = true;
  view.errorText.textContent = "";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll one job until it is final; a request that passed validation always has a job id, so
 *  this is the only path from Generate to a video (SPEC.md section 6). */
async function waitForJob(jobId) {
  for (;;) {
    const job = await requestJson(ENDPOINTS.job + encodeURIComponent(jobId));
    if (FINAL_STATUSES.has(job.status)) {
      return job;
    }
    setStatus(t(`status.${job.status}`));
    await sleep(POLL_MS);
  }
}

async function showVideo(url) {
  view.playerEmpty.hidden = true;
  view.player.hidden = false;
  view.player.src = url;
  setStatus(t("status.done"));
  try {
    await view.player.play();
  } catch {
    // A browser that will not start a video by itself still leaves the player ready to use.
    setStatus(t("status.press_play"));
  }
}

async function generate() {
  hideError();
  setBusy(true);
  setStatus(t("status.queued"));
  try {
    const accepted = await requestJson(ENDPOINTS.render, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(currentParams()),
    });
    if (accepted.cached) {
      setStatus(t("status.cached"));
    }
    const job = await waitForJob(accepted.job_id);
    if (job.status === "done") {
      await showVideo(job.video_url);
    } else {
      showError(job.error);
    }
  } catch (failure) {
    showError(failure.api ?? { code: "unexpected", params: {} });
  } finally {
    setBusy(false);
  }
}

function cacheElements() {
  for (const [name, id] of Object.entries(ELEMENT_IDS)) {
    view[name] = document.getElementById(id);
  }
  view.output = document.querySelector(".pane--output");
}

async function boot() {
  cacheElements();
  await loadLocale(DEFAULT_LOCALE);
  applyText(document);

  try {
    options = await requestJson(ENDPOINTS.options);
  } catch (failure) {
    showError(failure.api);
    return;
  }

  view.appName.textContent = options.app.name;
  applyDocumentTitle(options.app.name);
  buildForm();
  setStatus(t("status.ready"));
  view.settings.hidden = false;

  // The browser enforces the min, max and maxlength this form was built with, and
  // `reportValidity` shows the complaint and moves focus to the first bad field.
  view.settings.addEventListener("submit", (event) => {
    event.preventDefault();
    if (view.settings.reportValidity()) {
      generate();
    }
  });
}

boot();
