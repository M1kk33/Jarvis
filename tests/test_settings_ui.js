"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/settings.js"), "utf8");

function fixture() {
  const elements = new Map();
  let focused;
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", hidden: false, handlers: {}, dataset: {}, style: { setProperty(key, value) { this[key] = value; } },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      setAttribute(key, value) { this[key] = value; },
      showModal() { this.open = true; }, close() { this.open = false; this.handlers.close?.(); }, focus() { focused = this; },
    });
    return elements.get(id);
  };
  const tabs = ["appearance", "voice", "model"].map(id => { const el = get(`tab-${id}`); el.dataset.settingsTab = id; return el; });
  const pages = ["appearance", "voice", "model"].map(id => { const el = get(`page-${id}`); el.dataset.settingsPage = id; return el; });
  const root = get("root");
  const media = { matches: false, addEventListener(name, handler) { this.handler = handler; } };
  const window = { matchMedia: () => media };
  const document = { getElementById: get, documentElement: root, querySelectorAll: selector => selector === "[data-settings-tab]" ? tabs : pages };
  vm.runInNewContext(source, { window, document });
  let saved = { theme: "dark", accent: "mint", text_size: 14, send_shortcut: "enter", voice_volume: 80, output_tokens: 2048 };
  let fail = false;
  let deferRead;
  const applied = [];
  const writes = [];
  const result = () => ({ preferences: { ...saved }, model_name: "Qwen installed", context_tokens: 4096, active_output_tokens: 2048, restart_required: saved.output_tokens !== 2048 });
  const settings = new window.JarvisSettings({
    api: async (_url, method, preferences) => {
      if (fail) throw new Error("Cannot save");
      if (method === "POST") { saved = { ...preferences }; writes.push(saved); }
      else if (deferRead) return deferRead;
      return result();
    },
    onApply: p => applied.push({ ...p }),
  });
  return { settings, get, root, media, applied, writes, result, focused: () => focused, fail: value => { fail = value; }, defer: value => { deferRead = value; } };
}

test("settings save applies only committed values and keeps restart status explicit", async () => {
  const f = fixture();
  await f.settings.initialize({ settings_enabled: true, spoken_enabled: true });
  await f.settings.open();
  f.get("setting-theme").value = "light";
  f.get("setting-output-tokens").value = "1024";
  f.get("setting-voice-volume").value = "35";
  assert.equal(f.root.dataset.theme, "dark");
  await f.settings.save();
  assert.equal(f.root.dataset.theme, "light");
  assert.equal(f.applied.at(-1).voice_volume, 35);
  assert.equal(f.writes.at(-1).output_tokens, 1024);
  assert.match(f.get("settings-status").textContent, /Перезапустите/);
  f.get("setting-theme").value = "dark";
  f.fail(true);
  await f.settings.save();
  assert.equal(f.root.dataset.theme, "light");
  assert.equal(f.get("settings-status").textContent, "Cannot save");
  assert.equal(f.get("save-settings").disabled, false);
});

test("closing discards unsaved edits and resetting needs an explicit save", async () => {
  const f = fixture();
  await f.settings.initialize({ settings_enabled: true });
  await f.settings.open();
  f.get("setting-theme").value = "light";
  f.settings.close();
  assert.equal(f.focused(), f.get("open-settings"));
  await f.settings.open();
  assert.equal(f.get("setting-theme").value, "dark");
  f.get("reset-settings").handlers.click();
  assert.equal(f.writes.length, 0);
  assert.equal(f.get("setting-voice-volume").disabled, true);
});

test("system appearance responds to Windows theme changes and stale reads are inert", async () => {
  const f = fixture();
  await f.settings.initialize({ settings_enabled: true });
  await f.settings.open();
  f.get("setting-theme").value = "system";
  f.get("setting-accent").value = "blue";
  f.get("setting-text-size").value = "18";
  await f.settings.save();
  f.media.matches = true;
  f.media.handler();
  assert.equal(f.root.dataset.theme, "light");
  assert.equal(f.root.dataset.accent, "blue");
  assert.equal(f.root.style["--chat-text-size"], "18px");
  f.settings.close();
  let release;
  f.defer(new Promise(resolve => { release = resolve; }));
  const opening = f.settings.open();
  f.settings.close();
  release({ ...f.result(), preferences: { ...f.result().preferences, theme: "dark" } });
  await opening;
  assert.equal(f.root.dataset.theme, "light");
});

test("a failed initial read can be retried from the settings button", async () => {
  const f = fixture();
  f.fail(true);
  await f.settings.initialize({ settings_enabled: true });
  assert.equal(f.get("open-settings").hidden, false);
  await f.settings.open();
  assert.equal(f.get("save-settings").disabled, true);
  f.settings.close();
  f.fail(false);
  await f.settings.open();
  assert.equal(f.get("save-settings").disabled, false);
});
