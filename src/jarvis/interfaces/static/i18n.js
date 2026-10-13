"use strict";

window.JarvisI18n = (() => {
  const dictionary = JSON.parse(document.getElementById("translations")?.textContent || "{}");
  let language = document.documentElement.lang === "en" ? "en" : "ru";
  function t(source, ...values) {
    const key = Array.isArray(source) ? source.map((part, index) => part + (index < values.length ? `{${index}}` : "")).join("") : source;
    const text = language === "en" && Object.hasOwn(dictionary, key) ? dictionary[key] : key;
    return String(text).replace(/\{(\d+)\}/g, (match, index) => index < values.length ? String(values[index]) : match);
  }
  function apply() {
    document.documentElement.lang = language;
    for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
    for (const attribute of ["title", "aria-label", "placeholder", "alt"]) {
      for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
    }
  }
  function setLanguage(value) {
    const next = value === "en" ? "en" : "ru";
    if (next === language) return false;
    language = next;
    apply();
    return true;
  }
  apply();
  return { t, setLanguage, locale: () => language === "en" ? "en-US" : "ru-RU" };
})();
