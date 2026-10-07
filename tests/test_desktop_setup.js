"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/app.js"), "utf8");

function fixture(hash, ok = true) {
  const events = [];
  const token = { value: "" };
  const notice = { textContent: "" };
  const button = { disabled: false };
  let submit;
  const form = {
    querySelector: () => button,
    addEventListener: (_name, callback) => { submit = callback; },
  };
  const window = {
    location: { hash, pathname: "/setup", search: "", replace: (url) => events.push(["navigate", url]) },
    history: { replaceState: (_state, _title, url) => events.push(["history", url]) },
  };
  vm.runInNewContext(source, {
    window, URLSearchParams,
    document: { getElementById: (id) => ({ notice, "setup-form": form, "setup-token": token })[id] },
    fetch: async (url, options) => {
      events.push(["fetch", url, JSON.parse(options.body)]);
      return { ok, status: ok ? 200 : 403, json: async () => ({ detail: "Invalid token" }) };
    },
  });
  return { events, token, notice, button, submit: () => submit({ preventDefault() {} }) };
}

test("launcher removes secret from history before exchanging it and clears input", async () => {
  const f = fixture("#token=one-use-secret");
  await new Promise(setImmediate);
  assert.deepEqual(f.events, [
    ["history", "/setup"],
    ["fetch", "/api/setup", { token: "one-use-secret" }],
    ["navigate", "/"],
  ]);
  assert.equal(f.token.value, "");
});

test("a failed automatic exchange leaves manual sign-in usable", async () => {
  const f = fixture("#token=expired", false);
  await new Promise(setImmediate);
  assert.equal(f.notice.textContent, "Invalid token");
  assert.equal(f.button.disabled, false);
  f.token.value = "manual-token";
  await f.submit();
  assert.equal(f.events.at(-1)[2].token, "manual-token");
});

test("terminal setup never submits a token without the user", async () => {
  const f = fixture("");
  await new Promise(setImmediate);
  assert.deepEqual(f.events, []);
  f.token.value = "manual-token";
  await f.submit();
  assert.equal(f.events[0][2].token, "manual-token");
});
