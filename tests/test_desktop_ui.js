"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../src/jarvis/interfaces/static/app.js"), "utf8");

function fixture({ vision = false, decode, history, conversations: initialConversations = [] } = {}) {
  class Element {
    constructor() { this.children = []; this.handlers = {}; this.hidden = true; this.value = ""; this.files = []; this.textContent = ""; this.classList = { add() {}, remove() {} }; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    addEventListener(name, handler) { this.handlers[name] = handler; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    focus() {}
    requestSubmit() { return this.handlers.submit({ preventDefault() {} }); }
  }
  const elements = new Map();
  const get = (id) => {
    if (id === "setup-form") return null;
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const requests = [];
  let conversations = [...initialConversations];
  let createdConversations = 0;
  let approvals = { approvals: [], uncertain_actions: [], uncertain_files: [] };
  let failSend = false;
  let jobs = [];
  const sources = [];
  const handlers = {};
  const canvases = [];
  const revoked = [];
  const document = { getElementById: get, createElement: (tag) => {
    const el = new Element();
    if (tag === "canvas") {
      el.getContext = () => ({ drawImage() {} });
      el.toBlob = (callback) => callback(new Blob(["png-test"], { type: "image/png" }));
      canvases.push(el);
    }
    return el;
  }, addEventListener(name, handler) { handlers[name] = handler; } };
  const window = {
    location: { hash: "", pathname: "/", search: "" }, addEventListener() {},
    JarvisVoicePlayer: class { stop() {} },
  };
  vm.runInNewContext(source, {
    window, document, URLSearchParams, crypto: { randomUUID }, Blob, File,
    URL: { createObjectURL: () => "blob:preview", revokeObjectURL: (url) => revoked.push(url) },
    createImageBitmap: decode || (async () => ({ width: 1920, height: 1080, close() {} })),
    FileReader: class {
      readAsDataURL(file) { this.result = "data:image/png;base64,cG5nLXRlc3Q="; this.onload(); }
    },
    EventSource: class {
      constructor() { this.handlers = {}; sources.push(this); }
      addEventListener(name, handler) { this.handlers[name] = handler; }
      close() { this.closed = true; }
    },
    fetch: async (url, options = {}) => {
      const payload = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, method: options.method, payload });
      let value;
      let ok = true;
      if (url === "/api/session") value = { csrf_token: "test", speech_enabled: false, vision_enabled: vision };
      else if (url === "/api/conversations" && options.method === "POST") {
        value = { id: `conversation-${++createdConversations}`, title: payload.title };
        conversations = [value, ...conversations];
      } else if (url === "/api/conversations") value = { conversations };
      else if (url === "/api/jobs") value = { jobs };
      else if (url === "/api/approvals") value = approvals;
      else if (url.endsWith("/messages") || url.endsWith("/vision")) {
        value = failSend ? { detail: "Try again" } : { job: { id: "job-1" } };
        ok = !failSend;
      } else value = { conversation: conversations[0], messages: [], turns: [], drafts: [], ...history };
      return { ok, status: ok ? 200 : 503, json: async () => value };
    },
  });
  return { get, requests, handlers, canvases, revoked, sources, setJobs(value) { jobs = value; }, setApprovals(value) { approvals = value; }, failSend(value) { failSend = value; } };
}
const settle = () => new Promise(setImmediate);

test("new conversations appear at the top and reveal it without resetting older-chat browsing", async () => {
  const f = fixture({ conversations: [
    { id: "recent", title: "Recent" }, { id: "old", title: "Old" },
  ] });
  await settle();
  const nav = f.get("conversations");
  assert.deepEqual(nav.children.map((item) => item.textContent), ["Recent", "Old"]);
  assert.equal(nav.children[0].className, "selected");
  assert.ok(f.requests.some((r) => r.url === "/api/conversations/recent"));
  nav.scrollTop = 400;
  await nav.children[1].handlers.click();
  assert.equal(nav.scrollTop, 400);
  assert.equal(nav.children[1].className, "selected");
  await f.get("new-conversation").handlers.click();
  assert.equal(nav.scrollTop, 0);
  nav.scrollTop = 400;
  f.get("message-input").value = "Newest";
  await f.get("send-form").requestSubmit();
  assert.equal(nav.scrollTop, 0);
  assert.deepEqual(nav.children.map((item) => item.textContent), ["Newest", "Recent", "Old"]);
  assert.equal(nav.children[0].className, "selected");
  await f.get("refresh").handlers.click();
  assert.equal(nav.children[0].textContent, "Newest");
});

test("image progress updates the message and ignores stale closed streams", async () => {
  const f = fixture({ history: {
    messages: [{ id: "message-1", turn_id: "turn-1", role: "user", text: "Cat" }],
    turns: [{ id: "turn-1", status: "pending" }],
  } });
  await settle();
  const job = { id: "job-1", conversation_id: "conversation-1", turn_id: "turn-1", status: "running", progress: "image_loading", updated_at: "2026-10-09T10:00:00Z" };
  f.setJobs([job]);
  f.get("message-input").value = "Cat";
  await f.get("send-form").requestSubmit();
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  assert.match(strings(f.get("messages")), /Подготавливаю модель изображений/);
  const source = f.sources.at(-1);
  const newer = { ...job, progress: "image_generating", updated_at: "2026-10-09T10:00:01Z" };
  await source.handlers.progress({ data: JSON.stringify({ job: newer }) });
  // /api/jobs still returns the older snapshot; it must not undo the SSE update.
  assert.match(strings(f.get("messages")), /Генерирую изображение/);
  await f.get("new-conversation").handlers.click();
  await source.handlers.progress({ data: JSON.stringify({ job: newer, draft: "stale draft" }) });
  assert.equal(f.get("draft").textContent, "");
  assert.equal(f.get("conversation-title").textContent, "Новый диалог");
});

test("first message creates one conversation and failed send keeps submission identity", async () => {
  const f = fixture();
  await settle();
  assert.equal(f.get("messages").children[0].className, "welcome");
  f.get("message-input").value = "Привет";
  f.failSend(true);
  await f.get("send-form").requestSubmit();
  const first = f.requests.find((r) => r.url.endsWith("/messages"));
  assert.equal(f.get("message-input").value, "Привет");
  f.failSend(false);
  await f.get("send-form").requestSubmit();
  const sends = f.requests.filter((r) => r.url.endsWith("/messages"));
  assert.equal(sends.length, 2);
  assert.equal(sends[1].payload.submission_id, first.payload.submission_id);
  assert.equal(f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST").length, 1);
  assert.equal(f.get("message-input").value, "");
});

test("new uncertain outcomes open attention panel; closing it does not acknowledge them", async () => {
  const f = fixture();
  await settle();
  f.setApprovals({ approvals: [], uncertain_actions: [{ id: "uncertain-1", name: "generate_image", target_identity: "local", arguments_json: "{}" }] });
  await f.get("refresh").handlers.click();
  assert.equal(f.get("activity-panel").hidden, false);
  assert.equal(f.get("approval-count").textContent, "1");
  f.get("close-activity").handlers.click();
  await f.get("refresh").handlers.click();
  assert.equal(f.get("activity-panel").hidden, true);
  assert.equal(f.get("approval-count").hidden, false);
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
});

test("Enter sends; Shift+Enter and IME composition do not", async () => {
  const f = fixture();
  await settle();
  let submitted = 0;
  f.get("send-form").requestSubmit = () => { submitted++; };
  const press = (extra) => f.get("message-input").handlers.keydown({ key: "Enter", preventDefault() {}, ...extra });
  press({ shiftKey: true });
  press({ isComposing: true });
  press({ keyCode: 229 });
  assert.equal(submitted, 0);
  press({});
  assert.equal(submitted, 1);
  f.get("send-button").disabled = true;
  press({});
  assert.equal(submitted, 1);
});

test("new conversation stays empty through refresh and gets its first message as title", async () => {
  const f = fixture();
  await settle();
  f.get("message-input").value = "First conversation";
  await f.get("send-form").requestSubmit();
  await f.get("new-conversation").handlers.click();
  await f.get("refresh").handlers.click();
  assert.equal(f.get("conversation-title").textContent, "Новый диалог");
  assert.equal(f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST").length, 1);
  f.get("message-input").value = "Second conversation";
  await f.get("send-form").requestSubmit();
  const creates = f.requests.filter((r) => r.url === "/api/conversations" && r.method === "POST");
  assert.equal(creates.length, 2);
  assert.equal(creates[1].payload.title, "Second conversation");
});

test("pasted screenshot is resized, previewed, and submitted through vision", async () => {
  const f = fixture({ vision: true });
  await settle();
  let prevented = false;
  f.get("send-form").handlers.paste({
    clipboardData: { items: [{ kind: "file", type: "image/png", getAsFile: () => new File(["pixels"], "clip.png", { type: "image/png" }) }] },
    preventDefault() { prevented = true; },
  });
  await settle();
  assert.equal(prevented, true);
  assert.equal(f.get("screenshot-preview").hidden, false);
  assert.equal(f.get("vision-controls").open, true);
  assert.equal(f.canvases[0].width, 1024);
  assert.equal(f.canvases[0].height, 576);
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
  f.get("message-input").value = "What is shown?";
  await f.get("send-form").requestSubmit();
  const sent = f.requests.find((r) => r.url.endsWith("/vision"));
  assert.equal(sent.payload.question, "What is shown?");
  assert.equal(sent.payload.image_base64, "cG5nLXRlc3Q=");
  assert.equal(f.get("screenshot-preview").hidden, true);
  assert.equal(f.revoked.length, 1);
});

test("text paste is untouched and dropped JPEG can be removed before sending", async () => {
  const f = fixture({ vision: true });
  await settle();
  f.get("send-form").handlers.paste({ clipboardData: { items: [{ kind: "string", type: "text/plain" }] }, preventDefault() { throw new Error("Text paste intercepted"); } });
  f.handlers.drop({ dataTransfer: { files: [new File(["pixels"], "photo.jpg", { type: "image/jpeg" })] }, preventDefault() {} });
  await settle();
  assert.equal(f.get("screenshot-preview").hidden, false);
  f.get("remove-screenshot").handlers.click();
  assert.equal(f.get("screenshot-preview").hidden, true);
  f.get("message-input").value = "Text only";
  await f.get("send-form").requestSubmit();
  assert.equal(f.requests.some((r) => r.url.endsWith("/vision")), false);
});

test("unfinished screenshot conversion cannot attach to another conversation", async () => {
  let finish;
  const f = fixture({ vision: true, decode: () => new Promise((resolve) => { finish = resolve; }) });
  await settle();
  f.handlers.drop({ dataTransfer: { files: [new File(["pixels"], "a.png", { type: "image/png" })] }, preventDefault() {} });
  f.get("message-input").value = "Not ready";
  await f.get("send-form").requestSubmit();
  assert.equal(f.requests.filter((r) => r.method === "POST").length, 0);
  await f.get("new-conversation").handlers.click();
  finish({ width: 100, height: 100, close() {} });
  await settle();
  assert.equal(f.get("screenshot-preview").hidden, true);
});

test("image admission failure explains actual memory and keeps technical details", async () => {
  const f = fixture({ history: {
    messages: [{ id: "message-1", turn_id: "turn-1", role: "user", text: "Cat" }],
    turns: [{ id: "turn-1", status: "failed" }],
    invocations: [{ turn_id: "turn-1", name: "generate_image", status: "complete",
      result_json: JSON.stringify({ status: "error", code: "out_of_memory", memory: { resource: "ram", available_gib: 6.5, required_gib: 7 } }) }],
  } });
  await settle();
  f.get("message-input").value = "Cat";
  await f.get("send-form").requestSubmit();
  const strings = (node) => [node.textContent, ...node.children.flatMap(strings)].join(" ");
  const rendered = strings(f.get("messages"));
  assert.match(rendered, /7\.0 ГиБ свободной ОЗУ/);
  assert.match(rendered, /было 6\.5 ГиБ/);
  assert.match(rendered, /generate_image: не выполнено/);
  assert.match(rendered, /out_of_memory/);
});
