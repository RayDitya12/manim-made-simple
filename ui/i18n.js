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
 * ROADMAP 3.1 set the mechanism up. ROADMAP 3.5 added the automatic choice from the system
 * locale (`pickLocale` and `systemLanguages` below), the switcher in `app.js`, and the full
 * error-code copy in the locale files.
 */

/** Locales the app ships, and the file each one is served from. */
export const LOCALE_FILES = {
  en: "/locales/en.json",
  id: "/locales/id.json",
};

/** The locale used when nothing points anywhere else: the language of the source and of the
 *  fallback `message` every SPEC.md section 6 error object carries. */
export const DEFAULT_LOCALE = "en";

/**
 * How each locale names itself to the user.
 *
 * An option in a language picker is always the language's own name, in that language: a person
 * switching language is telling us which they read, not which language the label should be
 * translated into. So this stays in the code, out of the locale files, the same way font names
 * do (SPEC.md section 8).
 */
export const NATIVE_NAMES = {
  en: "English",
  id: "Bahasa Indonesia",
};

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

/**
 * The language tags the machine reports, most preferred first (SPEC.md section 8).
 *
 * Inside Electron this is the hook the main process puts on the page through `preload.js`
 * (ROADMAP 6.1: `app.getLocale()` and `app.getSystemLocale()`), which exists in a web browser
 * too as `navigator.languages`, so the browser path needs no bridge. Both are read here, and
 * the order is only a preference of detail: in the desktop app the two answer the same.
 */
export function systemLanguages() {
  const sources = [
    window.mtsSystemLanguages,
    navigator.languages,
    navigator.language,
  ];
  for (const source of sources) {
    const tags = Array.isArray(source) ? source : [source];
    const known = tags.filter((tag) => typeof tag === "string" && tag !== "");
    if (known.length) {
      return known;
    }
  }
  return [];
}

/**
 * The locale a list of system language tags selects.
 *
 * SPEC.md section 8: any tag beginning with `id` selects Indonesian, everything else falls back
 * to English. A tag is matched on its language subtag, so `id`, `id-ID`, `id-ID-u-nu-latn` and
 * an unexpected `in-ID` all agree with the browser's own negotiation.
 */
export function pickLocale(tags) {
  const indonesian = tags.some(
    (tag) => /^id(?:-|_|$)/i.test(tag) || /^in(?:-|_|$)/i.test(tag),
  );
  return indonesian ? "id" : DEFAULT_LOCALE;
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
 *
 * Any code starting with `http` is not stable enough for its own key: `http_<status>` names a response the backend
 * produced without a JSON error object of its own, so it reads the shared `error.http` line,
 * which shows the number, before the fallback ladder is reached.
 */
export function errorText(error) {
  const params = {};
  for (const [name, value] of Object.entries(error?.params ?? {})) {
    params[name] = typeof value === "number" ? number(value) : value;
  }
  let translated = error?.code ? lookup(`error.${error.code}`) : undefined;
  if (translated === undefined && String(error?.code).startsWith("http")) {
    translated = lookup("error.http");
  }
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
