"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/settings.js"), "utf8");

function fixture(metadata = {}) {
  const elements = new Map();
  let focused;
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", hidden: false, handlers: {}, dataset: {}, style: { setProperty(key, value) { this[key] = value; } }, children: [],
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, handler) { this.handlers[name] = handler; },
      setAttribute(key, value) { this[key] = value; },
      showModal() { this.open = true; }, close() { this.open = false; this.handlers.close?.(); }, focus() { focused = this; },
    });
    return elements.get(id);
  };
  const tabs = ["appearance", "voice", "model", "statistics", "internet"].map(id => { const el = get(`tab-${id}`); el.dataset.settingsTab = id; return el; });
  const pages = ["appearance", "voice", "model", "statistics", "internet"].map(id => { const el = get(`page-${id}`); el.dataset.settingsPage = id; return el; });
  const root = get("root");
  const media = { matches: false, addEventListener(name, handler) { this.handler = handler; } };
  const window = { matchMedia: () => media };
  const document = { getElementById: get, createElement: () => ({ value: "", textContent: "" }), documentElement: root, querySelectorAll: selector => selector === "[data-settings-tab]" ? tabs : pages };
  vm.runInNewContext(source, { window, document });
  let saved = { theme: "dark", accent: "mint", text_size: 14, send_shortcut: "enter", voice_volume: 80, output_tokens: 2048 };
  let fail = false;
  let deferRead;
  const applied = [];
  const writes = [];
  const requests = [];
  let statistics = { conversations: 2, user_messages: 3, assistant_messages: 4, input_tokens: 20, output_tokens: null, counted_user_messages: 1, counted_assistant_messages: 0, regenerations: 1, generated_images: 2, failed_turns: 1, interrupted_turns: 2, pending_turns: 0, first_message_at: null, last_message_at: null };
  const result = () => ({ preferences: { ...saved }, model_name: "Qwen installed", context_tokens: 4096, active_output_tokens: 2048, ...metadata, restart_required: saved.output_tokens !== 2048 || (saved.enable_network ?? Boolean(metadata.active_network)) !== Boolean(metadata.active_network) || (saved.enable_browser ?? Boolean(metadata.active_browser)) !== Boolean(metadata.active_browser) });
  const settings = new window.JarvisSettings({
    api: async (_url, method, preferences) => {
      requests.push(_url);
      if (fail) throw new Error("Cannot save");
      if (_url === "/api/conversations") return { conversations: [{ id: "old", title: "<img onerror=bad()>" }, { id: "current", title: "Now" }] };
      if (_url.startsWith("/api/statistics")) return statistics;
      if (method === "POST") { saved = { ...preferences }; writes.push(saved); }
      else if (deferRead) return deferRead;
      return result();
    },
    onApply: p => applied.push({ ...p }),
    getSelectedConversation: () => "current",
  });
  return { settings, get, root, media, applied, writes, result, requests, statistics: value => { statistics = value; }, focused: () => focused, fail: value => { fail = value; }, defer: value => { deferRead = value; } };
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

test("statistics stays on demand, shows exact coverage and safe chat choices without saving preferences", async () => {
  const f = fixture();
  await f.settings.initialize({ settings_enabled: true, statistics_enabled: true });
  await f.settings.open();
  assert.equal(f.requests.some(url => url.startsWith("/api/statistics")), false);
  f.get("tab-statistics").handlers.click();
  await f.settings.openStatistics();
  assert.equal(f.get("statistics-results").hidden, false);
  assert.equal(f.get("statistics-input-tokens").textContent, "20");
  assert.equal(f.get("statistics-output-tokens").textContent, "Нет данных");
  assert.match(f.get("statistics-coverage").textContent, /1 из 3 сообщений/);
  assert.equal(f.get("statistics-unfinished").textContent, "3");
  assert.equal(f.get("statistics-scope").children[1].value, "current");
  assert.equal(f.get("statistics-scope").children[2].textContent, "<img onerror=bad()>");
  assert.equal(f.get("save-settings").hidden, true);
  await f.settings.save();
  assert.equal(f.writes.length, 0);
  f.get("statistics-scope").value = "a&b";
  await f.settings.loadStatistics();
  assert.equal(f.requests.at(-1), "/api/statistics?conversation_id=a%26b");
  f.get("tab-appearance").handlers.click();
  assert.equal(f.get("save-settings").hidden, false);
});

test("closing or switching scope rejects old statistics; failure hides stale totals and permits retry", async () => {
  const f = fixture();
  await f.settings.initialize({ settings_enabled: true, statistics_enabled: true });
  await f.settings.open();
  f.get("tab-statistics").handlers.click();
  await f.settings.openStatistics();
  let release;
  f.statistics(new Promise(resolve => { release = resolve; }));
  const loading = f.settings.loadStatistics();
  f.settings.close();
  release({});
  await loading;
  assert.equal(f.get("statistics-results").hidden, true);
  f.statistics({ conversations: 0, user_messages: 0, assistant_messages: 0, input_tokens: null, output_tokens: null, counted_user_messages: 0, counted_assistant_messages: 0, regenerations: 0, generated_images: 0, failed_turns: 0, interrupted_turns: 0, pending_turns: 0, first_message_at: null, last_message_at: null });
  await f.settings.open();
  await f.settings.openStatistics();
  assert.equal(f.get("statistics-input-tokens").textContent, "0");
  assert.equal(f.get("statistics-empty").hidden, false);
  f.fail(true);
  await f.settings.loadStatistics();
  assert.equal(f.get("statistics-results").hidden, true);
  assert.equal(f.get("statistics-status").textContent, "Cannot save");
  f.fail(false);
  await f.settings.openStatistics();
  assert.equal(f.get("statistics-results").hidden, false);
  assert.equal(f.writes.length, 0);
});


test("network controls retain unsaved choices until Save, enforce dependencies and show pending restart", async () => {
  const f = fixture({ browser_available: true, active_network: false, active_browser: false });
  await f.settings.initialize({ settings_enabled: true });
  await f.settings.open();
  const network = f.get("setting-enable-network"), browser = f.get("setting-enable-browser");
  assert.equal(network.checked, false);
  assert.equal(browser.disabled, true);
  network.checked = true; network.handlers.change();
  assert.equal(browser.disabled, false);
  browser.checked = true; browser.handlers.change();
  f.settings.close(); await f.settings.open();
  assert.equal(network.checked, false);
  network.checked = true; network.handlers.change(); browser.checked = true;
  f.fail(true); await f.settings.save();
  assert.equal(f.writes.length, 0);
  assert.equal(f.settings.preferences.enable_network, null);
  f.fail(false); await f.settings.save();
  assert.equal(f.writes[0].enable_network, true);
  assert.equal(f.writes[0].enable_browser, true);
  assert.equal(f.settings.restartRequired, true);
  assert.match(f.get("settings-status").textContent, /Перезапустите/);
  assert.match(f.get("setting-network-status").textContent, /выключено/);
  network.checked = false; network.handlers.change();
  assert.equal(browser.checked, false);
  assert.equal(browser.disabled, true);
  await f.settings.save();
  assert.equal(f.settings.restartRequired, false);
});

test("legacy launch grants remain visible and missing components cannot be newly enabled", async () => {
  const f = fixture({ browser_available: true, active_network: true, active_browser: true });
  await f.settings.initialize({ settings_enabled: true }); await f.settings.open();
  assert.equal(f.get("setting-enable-network").checked, true);
  assert.equal(f.get("setting-enable-browser").checked, true);
  await f.settings.save();
  assert.equal(f.writes[0].enable_network, true);
  assert.equal(f.settings.restartRequired, false);
  f.get("reset-settings").handlers.click();
  assert.equal(f.get("setting-enable-network").checked, false);
  assert.equal(f.get("setting-enable-browser").checked, false);
  const missing = fixture({ browser_available: false });
  await missing.settings.initialize({ settings_enabled: true }); await missing.settings.open();
  missing.get("setting-enable-network").checked = true; missing.get("setting-enable-network").handlers.change();
  assert.equal(missing.get("setting-enable-browser").disabled, true);
  assert.match(missing.get("setting-browser-status").textContent, /не установлен/);
});
