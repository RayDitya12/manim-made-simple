/**
 * The interface: options in, controls built, render requested, video played or saved
 * (ROADMAP tasks 3.1 and 3.2, SPEC.md section 8).
 *
 * Four rules shape this file:
 *
 * 1. The form is built from `GET /api/options`, never from numbers written here, so a limit
 *    changed in `backend/app/models.py` is the only change needed (SPEC.md section 5).
 * 2. Nothing reaches the DOM as markup. Elements are constructed and filled with
 *    `textContent`, so neither a translation nor the user's own text is ever parsed as HTML.
 * 3. Every string the user reads comes from `i18n.js`. The literals below that look like copy
 *    are the English fallbacks SPEC.md section 6 defines for an error object, and the locale's
 *    `error.*` key wins over each of them whenever it exists.
 * 4. Every request owns a token, so Cancel retires the loop that is waiting instead of racing
 *    it. The job itself is left running: SPEC.md section 6 says a render in progress is not
 *    interrupted by the queue, and its file still reaches the cache.
 */

import {
  DEFAULT_LOCALE,
  applyDocumentTitle,
  applyText,
  errorText,
  loadLocale,
  number,
  seconds,
  t,
} from "/i18n.js";

const ENDPOINTS = {
  options: "/api/options",
  render: "/api/render",
  job: "/api/jobs/",
  video: "/api/videos/",
};

/** The quality `Download HD` asks for whatever the Quality dropdown shows (SPEC.md section 8). */
const HD_QUALITY = "hd";

/** The query SPEC.md section 6 defines for a video served as an attachment. */
const DOWNLOAD_QUERY = "?download=1";

/** Where the last settings are remembered, under the slug the options endpoint reports so the
 *  name still lives in one place (SPEC.md section 3). Bump the suffix when a stored document
 *  can no longer be restored as it is. */
const SETTINGS_SUFFIX = "settings.v1";

/** How often a running job is polled. Render progress is not reliably known, so the UI shows
 *  a status sentence rather than a percentage (SPEC.md section 8). */
const POLL_MS = 400;

/** The statuses after which polling stops. */
const FINAL_STATUSES = new Set(["done", "failed"]);

/**
 * The media query the stylesheet already obeys. The same answer decides whether a finished
 * video starts by itself: a person who asked the system to reduce motion is not asking for a
 * clip to start moving in front of them, and `status.press_play` already tells them to press
 * play (SPEC.md section 8).
 */
const REDUCED_MOTION = "(prefers-reduced-motion: reduce)";

/**
 * How a slider's value is spoken. A range announces a bare number, which is no answer for a
 * duration, so a field that has a unit names one through `aria-valuetext`. The words come from
 * `Intl`, which owns the plural and the decimal separator with it (SPEC.md section 8), so they
 * are not copy in the locale files. An outline width is a plain number with no unit to name.
 */
const UNIT_FORMATTERS = { seconds };

/**
 * One entry per SPEC.md section 5 field: the control that edits it and the hint under it.
 * `limit` names the entry in `/api/options` that supplies min and max.
 *
 * `kind: "slider"` is the pair SPEC.md section 8 asks for: a range the user can drag and a
 * number they can type. The number box holds the submitted value, so browser validation and
 * `currentParams` work exactly as they do for a plain number field.
 */
const CONTROLS = {
  text: { kind: "textarea", hint: "hint.text" },
  animation: { kind: "select", hint: null },
  font: { kind: "select", hint: null },
  font_size: {
    kind: "number",
    limit: "font_size",
    step: 1,
    integer: true,
    hint: "hint.points",
  },
  animation_duration: {
    kind: "slider",
    limit: "animation_duration",
    step: 0.5,
    unit: "seconds",
    hint: "hint.seconds",
  },
  hold_duration: {
    kind: "slider",
    limit: "hold_duration",
    step: 0.5,
    unit: "seconds",
    hint: "hint.seconds",
  },
  text_color: { kind: "color", hint: null },
  outline_color: { kind: "color", hint: null },
  outline_width: {
    kind: "slider",
    limit: "outline_width",
    step: 0.5,
    unit: null,
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
  download: "download-hd",
  cancel: "cancel",
  retry: "retry",
  busy: "busy",
  error: "job-error",
  errorText: "job-error-text",
  player: "player",
  frame: "preview-frame",
  frameText: "preview-frame-text",
  frameMeta: "preview-meta",
};

let options = null;
const view = {};

/** Raised by every new request and by Cancel. A wait whose token is no longer the current one
 *  has been superseded, so it returns without touching the screen. */
let runToken = 0;

/** Which button `Try again` presses. The form is read again when it is, so the user can fix
 *  what the error complained about and press it without a second step (SPEC.md section 8). */
let lastAction = null;

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

function sliderId(name) {
  return `f-${name}-slider`;
}

function labelId(name) {
  return `l-${name}`;
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

/** The min and max `/api/options` advertises for one numeric field. */
function limitsOf(config) {
  return options.limits[config.limit];
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
    if (config.kind === "color") {
      control.type = "color";
    } else {
      control.type = "number";
      control.min = limitsOf(config).min;
      control.max = limitsOf(config).max;
      control.step = config.step;
    }
  }
  control.id = fieldId(name);
  control.name = name;
  control.value = valueFor(name);
  return control;
}

/**
 * The range beside a `kind: "slider"` field. It is named by the field's own visible label and
 * can only land on a value inside the advertised limits, so it never needs to report an error;
 * the number box next to it carries the submitted value and the browser's validation.
 */
function buildSlider(name, config) {
  const slider = document.createElement("input");
  slider.type = "range";
  slider.id = sliderId(name);
  slider.min = limitsOf(config).min;
  slider.max = limitsOf(config).max;
  slider.step = config.step;
  slider.value = valueFor(name);
  slider.setAttribute("aria-labelledby", labelId(name));
  return slider;
}

/** Write the value, in the field's own unit, where a screen reader can find it. */
function paintSlider(slider, config) {
  const say = config.unit ? UNIT_FORMATTERS[config.unit] : null;
  if (say) {
    slider.setAttribute("aria-valuetext", say(slider.valueAsNumber));
  }
}

/** Bring every slider back to the number box beside it, after a build or a restore. */
function syncSliders() {
  for (const [name, config] of Object.entries(CONTROLS)) {
    if (config.kind !== "slider") {
      continue;
    }
    const box = document.getElementById(fieldId(name));
    const slider = document.getElementById(sliderId(name));
    if (!Number.isNaN(box.valueAsNumber)) {
      slider.value = box.value;
    }
    paintSlider(slider, config);
  }
}

/**
 * One labelled control: `<label>`, the control (or the slider and number pair), and the
 * persistent helper text that states the limit (modern-web-guidance `forms`: keep constraints
 * visible, not placeholder-only). `groupHintId` adds a row-level hint without replacing the
 * field's own.
 */
function buildField(name, groupHintId) {
  const config = CONTROLS[name];
  const wrap = document.createElement("div");
  wrap.className = "field";

  const label = document.createElement("label");
  label.id = labelId(name);
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

  wrap.append(label);
  if (config.kind === "slider") {
    const slider = buildSlider(name, config);
    if (describedBy) {
      slider.setAttribute("aria-describedby", describedBy);
    }
    slider.addEventListener("input", () => {
      control.value = slider.value;
      paintSlider(slider, config);
    });
    control.addEventListener("input", () => {
      if (!Number.isNaN(control.valueAsNumber)) {
        slider.value = control.value;
      }
      paintSlider(slider, config);
    });
    paintSlider(slider, config);
    const pair = document.createElement("div");
    pair.className = "pair";
    pair.append(slider, control);
    wrap.append(pair);
  } else {
    wrap.append(control);
  }
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
  for (const name of [
    "text",
    "text_color",
    "outline_color",
    "background_color",
  ]) {
    document
      .getElementById(fieldId(name))
      .addEventListener("input", paintPreview);
  }
  document
    .getElementById(fieldId("quality"))
    .addEventListener("change", sizePlayer);
}

/**
 * Paint the frame the video will be drawn on: the background and text color that were picked,
 * and the line of text as it was typed. This is the same three values the render receives, so
 * the slate is the real output at the real shape rather than a picture of an idea of it. The
 * font is not mirrored, because the interface cannot show the fonts the renderer registers,
 * and the size here follows the frame, not the `font_size` field, which counts in the video's
 * own units. A custom property is the only thing this file writes to a style: the rules stay
 * in `styles.css`.
 */
function paintPreview() {
  view.output.style.setProperty(
    "--preview-background",
    valueOf("background_color"),
  );
  view.output.style.setProperty("--preview-color", valueOf("text_color"));
  view.frameText.textContent = valueOf("text");
}

/** Keep the player at the size the chosen quality produces, so the pane does not jump, and say
 *  under the frame what that quality is: its name, its shape, its frame rate. */
function sizePlayer() {
  const quality = options.qualities.find(
    (entry) => entry.id === valueOf("quality"),
  );
  if (!quality) {
    return;
  }
  view.player.width = quality.width;
  view.player.height = quality.height;
  view.output.style.setProperty(
    "--preview-ratio",
    `${quality.width} / ${quality.height}`,
  );
  view.frameMeta.textContent = [
    t(`option.quality.${quality.id}`),
    `${number(quality.width)} \u00d7 ${number(quality.height)}`,
    `${number(quality.fps)} fps`,
  ].join(" \u00b7 ");
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

/** What one button sends: `hd` fixes the quality, `generate` keeps the form's. */
function paramsFor(action) {
  const params = currentParams();
  return action === "hd" ? { ...params, quality: HD_QUALITY } : params;
}

function settingsKey() {
  return `${options.app.slug}.${SETTINGS_SUFFIX}`;
}

/** Remember the settings that were just accepted (SPEC.md section 8). Saving happens after the
 *  backend has validated the request, so only a set of values that can render comes back. */
function saveSettings() {
  try {
    localStorage.setItem(settingsKey(), JSON.stringify(currentParams()));
  } catch {
    // Storage the browser refuses, for instance in a private window, only costs the memory.
  }
}

/** The stored document, or `null`. What is in local storage is not trusted: another program,
 *  an older version, or an edited file can put anything there. */
function storedParams() {
  let text = null;
  try {
    text = localStorage.getItem(settingsKey());
  } catch {
    return null;
  }
  if (!text) {
    return null;
  }
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

/** The value one control may hold, or `null` to leave the default in place. Every rule is the
 *  one `/api/options` advertises, so the limits still live in `models.py` alone. */
function allowedValue(name, value) {
  const config = CONTROLS[name];
  if (config.kind === "select") {
    const known = choicesFor(name).some(
      (choice) => choice.value === value && choice.available !== false,
    );
    return known ? value : null;
  }
  if (config.kind === "textarea") {
    const limits = options.limits.text;
    const usable =
      typeof value === "string" &&
      value.trim() !== "" &&
      value.length <= limits.max_chars &&
      value.split("\n").length <= limits.max_lines &&
      !/[\x00-\x09\x0b-\x1f\x7f]/.test(value);
    return usable ? value : null;
  }
  if (config.kind === "color") {
    const pattern = new RegExp(options.hex_color_pattern);
    return typeof value === "string" && pattern.test(value) ? value : null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return null;
  }
  const range = limitsOf(config);
  if (value < range.min || value > range.max) {
    return null;
  }
  if (config.integer && !Number.isInteger(value)) {
    return null;
  }
  return String(value);
}

/** Put the remembered settings back into the form, field by field, keeping the advertised
 *  default wherever the stored value no longer fits. */
function restoreSettings() {
  const stored = storedParams();
  if (stored === null) {
    return;
  }
  for (const name of Object.keys(CONTROLS)) {
    const value =
      name === "background_type"
        ? stored.background?.type
        : name === "background_color"
          ? stored.background?.color
          : stored[name];
    const usable = allowedValue(name, value);
    if (usable !== null) {
      document.getElementById(fieldId(name)).value = usable;
    }
  }
}

function setStatus(text) {
  view.status.textContent = text;
}

/**
 * Both render buttons are disabled while a job runs (SPEC.md section 8), and a disabled button
 * takes focus out of the page, so the keyboard user's next Tab starts again at the heading.
 * Hand the caret to the control that replaced them and take it back when the row is idle,
 * without ever pulling it away from somewhere the user has already moved to.
 */
function setBusy(busy) {
  view.generate.disabled = busy;
  view.download.disabled = busy;
  view.cancel.hidden = !busy;
  view.busy.hidden = !busy;
  const next = busy ? view.cancel : view.generate;
  const active = document.activeElement;
  if (active === document.body || view.settings.contains(active)) {
    next.focus();
  }
}

/** Stop waiting for the render in flight and let the user work again. The job is not touched:
 *  its video still lands in the cache, so asking for the same settings again is served from
 *  there (SPEC.md sections 6 and 8). */
function cancelRender() {
  runToken += 1;
  setBusy(false);
  hideError();
  setStatus(t("status.cancelled"));
}

/** The status sentence for one job of one action. An HD render is slower and says so. */
function statusKey(action, jobStatus) {
  const waiting = jobStatus === "queued" || jobStatus === "running";
  return action === "hd" && waiting
    ? `status.hd_${jobStatus}`
    : `status.${jobStatus}`;
}

/**
 * Show the sentence for one SPEC.md section 6 error object. `action` names the button `Try
 * again` presses; an error nothing can retry, such as a backend that never answered at
 * start-up, hides that button rather than promising a retry it cannot make.
 */
function showError(api, action = null) {
  setStatus(t("status.failed"));
  view.errorText.textContent = errorText(api);
  lastAction = action;
  view.retry.hidden = action === null;
  view.error.hidden = false;
}

function hideError() {
  view.error.hidden = true;
  view.errorText.textContent = "";
  lastAction = null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Poll one job until it is final, or until the token is retired by Cancel, which answers
 *  `null`. A request that passed validation always has a job id, so this is the only path from
 *  either button to a video (SPEC.md section 6). */
async function waitForJob(jobId, token, action) {
  for (;;) {
    if (token !== runToken) {
      return null;
    }
    const job = await requestJson(ENDPOINTS.job + encodeURIComponent(jobId));
    if (token !== runToken) {
      return null;
    }
    if (FINAL_STATUSES.has(job.status)) {
      return job;
    }
    setStatus(t(statusKey(action, job.status)));
    await sleep(POLL_MS);
  }
}

/** Only the path this app's own video endpoint serves may become a player source or the
 *  target of a download link, so a URL from anywhere else is never used as one. */
function videoSource(url) {
  return typeof url === "string" && url.startsWith(ENDPOINTS.video)
    ? url
    : null;
}

async function showVideo(url) {
  const source = videoSource(url);
  if (source === null) {
    return false;
  }
  view.frame.hidden = true;
  view.player.hidden = false;
  view.player.src = source;
  setStatus(t("status.done"));
  if (window.matchMedia(REDUCED_MOTION).matches) {
    setStatus(t("status.press_play"));
    return true;
  }
  try {
    await view.player.play();
  } catch {
    // A browser that will not start a video by itself still leaves the player ready to use.
    setStatus(t("status.press_play"));
  }
  return true;
}

/**
 * Ask the browser to save the finished HD file. The endpoint already answers `?download=1`
 * with `Content-Disposition: attachment` (SPEC.md section 6); the link repeats that name so
 * the save still happens if the header is lost, and a clicked link keeps the page, the form
 * and whatever the player holds exactly where they are.
 */
function saveVideoFile(url) {
  const source = videoSource(url);
  if (source === null) {
    return false;
  }
  setStatus(t("status.saving"));
  const link = document.createElement("a");
  link.href = source + DOWNLOAD_QUERY;
  link.download = source.slice(ENDPOINTS.video.length);
  document.body.append(link);
  link.click();
  link.remove();
  return true;
}

/**
 * One render request, start to finish. `generate` plays the result in the page, `hd` saves it
 * to the user's machine; both share the request, the polling and the error line, so there is
 * one path through a render for the user and for the tests.
 */
async function startRender(action) {
  const token = ++runToken;
  hideError();
  setBusy(true);
  setStatus(t(statusKey(action, "queued")));
  try {
    const accepted = await requestJson(ENDPOINTS.render, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(paramsFor(action)),
    });
    if (token !== runToken) {
      return;
    }
    saveSettings();
    if (accepted.cached && action !== "hd") {
      setStatus(t("status.cached"));
    }
    const job = await waitForJob(accepted.job_id, token, action);
    if (job === null) {
      return;
    }
    if (job.status !== "done") {
      showError(job.error, action);
      return;
    }
    const shown =
      action === "hd"
        ? saveVideoFile(job.video_url)
        : await showVideo(job.video_url);
    if (!shown) {
      showError({ code: "video_not_found", params: {} }, action);
    }
  } catch (failure) {
    if (token !== runToken) {
      return;
    }
    showError(failure.api ?? { code: "unexpected", params: {} }, action);
  } finally {
    if (token === runToken) {
      setBusy(false);
    }
  }
}

/** Press whichever button runs a render, but only after the browser has checked the form. */
function requestRender(action) {
  if (view.settings.reportValidity()) {
    startRender(action);
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
  restoreSettings();
  syncSliders();
  paintPreview();
  sizePlayer();
  setStatus(t("status.ready"));
  view.settings.hidden = false;

  // The browser enforces the min, max and maxlength this form was built with, and
  // `reportValidity` shows the complaint and moves focus to the first bad field.
  view.settings.addEventListener("submit", (event) => {
    event.preventDefault();
    requestRender("generate");
  });
  view.download.addEventListener("click", () => requestRender("hd"));
  view.cancel.addEventListener("click", cancelRender);
  view.retry.addEventListener("click", () => requestRender(lastAction));
}

boot();
