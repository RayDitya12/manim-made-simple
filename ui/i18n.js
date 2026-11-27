/**
 * Locale loading and translation (SPEC.md section 8).
 *
 * Every string the user reads lives in `/locales/en.json` or `/locales/id.json`, and this
 * module is the only way the interface puts text on screen. Elements in the static markup
 * carry a `data-i18n` key and get filled here; strings built at run time go through `t()`.
 *
 * Nothing in this file writes markup: `textContent` and `title` only, so a translation can
 * never become an injection sink.
 *
 * ROADMAP 3.1 sets the mechanism up and starts every session in English. ROADMAP 3.5 adds the
 * automatic choice from the system locale, the switcher, and the full error-code copy.
 */

/** Locales the app ships, and the file each one is served from. */
export const LOCALE_FILES = {
  en: "/locales/en.json",
  id: "/locales/id.json",
};

/** The locale used until ROADMAP 3.5 decides one from the system. */
export const DEFAULT_LOCALE = "en";

let messages = {};
let current = DEFAULT_LOCALE;
let numbers = new Intl.NumberFormat(current);
let durations = new Intl.NumberFormat(current, {
  style: "unit",
  unit: "second",
  unitDisplay: "long",
});

/**
 * Fetch one locale file and make it the active one.
 *
 * Raises when the locale is unknown or the file cannot be read, which leaves the page on
 * English copy rather than half-translated.
 */
export async function loadLocale(locale) {
  const file = LOCALE_FILES[locale];
  if (!file) {
    throw new Error(`no locale file for ${locale}`);
  }
  const response = await fetch(file);
  if (!response.ok) {
    throw new Error(`locale ${locale} answered ${response.status}`);
  }
  const body = await response.json();
  messages = body;
  current = locale;
  numbers = new Intl.NumberFormat(locale);
  durations = new Intl.NumberFormat(locale, {
    style: "unit",
    unit: "second",
    unitDisplay: "long",
  });
  document.documentElement.lang = locale;
  return locale;
}

export function locale() {
  return current;
}

/** The raw string for a key, or `undefined` when the locale does not carry it. */
export function lookup(key) {
  const value = messages[key];
  return typeof value === "string" ? value : undefined;
}

/** Replace `{name}` placeholders. Values are inserted as text, never as markup. */
export function format(text, params) {
  if (!params) {
    return text;
  }
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.hasOwn(params, name) ? String(params[name]) : whole,
  );
}

/**
 * The copy for a key. A missing key comes back as the key itself, which is loud on screen and
 * is what the locale-parity test in `backend/tests/test_webui.py` exists to prevent.
 */
export function t(key, params) {
  const text = lookup(key);
  return text === undefined ? key : format(text, params);
}

/** Numbers and units go through `Intl` (SPEC.md section 8). */
export function number(value) {
  return numbers.format(value);
}

/**
 * A duration in the words this locale uses for it.
 *
 * `second` is an ICU unit, so the plural arrives with the number ("1 second", "2,5 detik")
 * instead of being guessed here or repeated in both locale files.
 */
export function seconds(value) {
  return durations.format(value);
}

/**
 * The sentence for one SPEC.md section 6 error object: the translation of its stable `code`
 * when the locale carries one, otherwise the English `message` the backend sent. The object's
 * `params` (limits, counts) fill the placeholders and go through `Intl`.
 */
export function errorText(error) {
  const params = {};
  for (const [name, value] of Object.entries(error?.params ?? {})) {
    params[name] = typeof value === "number" ? number(value) : value;
  }
  const translated = error?.code ? lookup(`error.${error.code}`) : undefined;
  return format(translated ?? error?.message ?? t("error.unexpected"), params);
}

/**
 * Fill every `data-i18n` element in a subtree, and `data-i18n-title` for tooltips.
 *
 * Called once at start-up for the static markup, and again after a locale change.
 */
export function applyText(root = document) {
  for (const node of root.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of root.querySelectorAll("[data-i18n-title]")) {
    node.title = t(node.dataset.i18nTitle);
  }
}

/**
 * The browser tab title: the app's name, which `app_meta.json` owns, plus the translated
 * description. The name is never written into a locale file, so it cannot drift from it.
 */
export function applyDocumentTitle(appName) {
  document.title = `${appName} — ${t("page.title")}`;
}
