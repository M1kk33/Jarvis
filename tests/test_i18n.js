"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const directory = path.join(__dirname, "../src/jarvis/interfaces/static");
const dictionary = JSON.parse(fs.readFileSync(path.join(directory, "translations.json"), "utf8"));

function fixture(language = "ru") {
  const label = { dataset: { i18n: "Настройки" }, textContent: "Настройки" };
  const input = { attributes: { "data-i18n-placeholder": "Спросить Jarvis" }, setAttribute(key, value) { this.attributes[key] = value; }, getAttribute(key) { return this.attributes[key]; } };
  const conversation = { textContent: "Настройки" };
  const root = { lang: language };
  const window = {};
  const document = { documentElement: root, getElementById: () => ({ textContent: JSON.stringify(dictionary) }), querySelectorAll: selector => selector === "[data-i18n]" ? [label] : selector === "[data-i18n-placeholder]" ? [input] : [] };
  vm.runInNewContext(fs.readFileSync(path.join(directory, "i18n.js"), "utf8"), { window, document });
  return { i18n: window.JarvisI18n, label, input, conversation, root };
}

test("language changes only marked interface labels and attributes", () => {
  const f = fixture();
  assert.equal(f.i18n.setLanguage("en"), true);
  assert.equal(f.root.lang, "en");
  assert.equal(f.label.textContent, "Settings");
  assert.equal(f.input.attributes.placeholder, "Ask Jarvis");
  assert.equal(f.conversation.textContent, "Настройки");
  assert.equal(f.i18n.locale(), "en-US");
  assert.equal(f.i18n.setLanguage("en"), false);
  f.i18n.setLanguage("ru");
  assert.equal(f.label.textContent, "Настройки");
  assert.equal(f.input.attributes.placeholder, "Спросить Jarvis");
});

test("saved English applies on initialization and interpolated data stays literal", () => {
  const f = fixture("en");
  assert.equal(f.label.textContent, "Settings");
  const data = '<img onerror=bad()> $& {0} Настройки';
  assert.equal(f.i18n.t(["Удалить диалог: ", ""], data), "Delete chat: " + data);
  assert.equal(f.i18n.t("unknown backend message"), "unknown backend message");
  assert.equal(f.i18n.t("__proto__"), "__proto__");
  assert.equal(f.i18n.t("Нет данных"), "No data");
});
